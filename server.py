#!/usr/bin/env python3
"""WiFi konum sunucusu: tarayıcıyı çalıştırır, ölçümleri saklar, konumu hesaplar, arayüzü sunar.

Çalıştırma: python3 server.py   ->  http://localhost:8765
"""
import atexit
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import locate

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
PORT = int(os.environ.get("PORT", "8765"))
MIN_LIVE = 6  # her ölçüm noktasında en az bu kadar anlık okuma toplanır
DEFAULT_RECTS = [
    {"name": "Oda", "x": 0.0, "y": 0.0, "w": 4.5, "h": 3.6},
    {"name": "Tuvalet", "x": 4.5, "y": 0.0, "w": 1.2, "h": 1.5},
]


class Bad(Exception):
    pass


def normalize(nets):
    """Tarama sonucunu {ağ anahtarı: dBm} haline getirir. Aynı anahtardan birden çok varsa en güçlüsü kalır."""
    out, named = {}, False
    for n in nets:
        rssi = n.get("rssi") or 0
        if rssi >= 0 or rssi < -100:
            continue
        ssid = (n.get("ssid") or "").strip()
        named = named or bool(ssid)
        key = f"{ssid or 'Gizli ağ'} · kanal {n.get('ch') or 0}"
        bssid = n.get("bssid") or ""
        if bssid:
            key += f" · {bssid[-5:]}"
        if key not in out or rssi > out[key]:
            out[key] = rssi
    return out, named


class Motion:
    """Bağlı ağın sinyalindeki dalgalanmadan hareket algılar (MacBook sabit dururken)."""

    WINDOW = 2.5    # dalgalanmanın ölçüldüğü pencere (sn)
    KEEP = 90.0     # grafikte tutulan geçmiş (sn)
    LEARN = 15.0    # sakin durumu öğrenme süresi (sn)
    FACTOR = {1: 2.2, 2: 1.6, 3: 1.25}  # hassasiyet -> eşik = sakin dalgalanma x katsayı

    def __init__(self):
        self.on = False
        self.last_poll = 0.0
        self.samples = deque()  # (zaman, dBm)
        self.sds = deque()      # (zaman, dalgalanma, hareket var mı)
        self.rest = None        # sakin durumdaki dalgalanma (dB)
        self.learn_start = None
        self.learn_vals = []
        self.sens = 2
        self.active = False
        self.below_since = None
        self.events = []

    def threshold(self):
        return max((self.rest if self.rest is not None else 1.5) * self.FACTOR[self.sens], 0.9)

    def learn(self):
        self.learn_start = time.time()
        self.learn_vals = []
        self.active = False

    def add(self, t0, t1, samples):
        n = len(samples)
        for i, v in enumerate(samples):
            self.samples.append((t0 + (t1 - t0) * (i + 0.5) / n, v))
        while self.samples and self.samples[0][0] < t1 - self.KEEP:
            self.samples.popleft()
        win = [v for t, v in self.samples if t >= t1 - self.WINDOW]
        if len(win) < 10:
            return
        sd = statistics.pstdev(win)
        if self.learn_start is not None:
            # İlk pencere, düğmeye basılmadan önceki hareketi içerebilir; onu sayma.
            if t1 >= self.learn_start + self.WINDOW:
                self.learn_vals.append(sd)
            if t1 >= self.learn_start + self.LEARN and self.learn_vals:
                vals = sorted(self.learn_vals)
                self.rest = vals[int(0.9 * (len(vals) - 1))]
                self.learn_start = None
        else:
            thr = self.threshold()
            if sd > thr:
                if not self.active:
                    self.active = True
                    self.events.append({"start": t1, "end": t1, "peak": sd})
                    self.events = self.events[-20:]
                ev = self.events[-1]
                ev["end"] = t1
                ev["peak"] = max(ev["peak"], sd)
                self.below_since = None
            elif self.active:
                if sd < thr * 0.8:
                    self.below_since = self.below_since or t1
                    if t1 - self.below_since >= 1.5:
                        self.active = False
                else:
                    self.below_since = None
        self.sds.append((t1, sd, self.active))
        while self.sds and self.sds[0][0] < t1 - self.KEEP:
            self.sds.popleft()

    def snapshot(self):
        now = time.time()
        return {
            "on": self.on,
            "active": self.active,
            "sd": round(self.sds[-1][1], 2) if self.sds else None,
            "thr": round(self.threshold(), 2),
            "rest": None if self.rest is None else round(self.rest, 2),
            "sens": self.sens,
            "learning": None if self.learn_start is None else round(max(0.0, self.learn_start + self.LEARN - now), 1),
            "series": [[round(now - t, 2), v] for t, v in self.samples if now - t <= 60],
            "sds": [[round(now - t, 2), round(sd, 2), 1 if a else 0] for t, sd, a in self.sds if now - t <= 60],
            "events": [
                {"ago": round(now - e["end"]), "dur": round(e["end"] - e["start"], 1), "peak": round(e["peak"], 1)}
                for e in reversed(self.events[-8:])
            ],
        }


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.rects = [dict(r) for r in DEFAULT_RECTS]
        self.points = []
        self.next_id = 1
        self.rev = 0
        self.model = None
        self.filter = None
        self.est = None
        self.last_update = None
        self.scan = None
        self.cur = None
        self.period = 13.3
        self.scan_dur = 3.2
        self.collecting = None
        self.err = None
        self.no_names = False
        self.proc = None
        self.motion = Motion()
        self.load()
        self.rebuild()

    # ---- kalıcı veri -------------------------------------------------------
    def load(self):
        try:
            with open(DATA, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.rects = d.get("rects") or self.rects
        self.points = [p for p in d.get("points", []) if p.get("scans")]
        for p in self.points:
            p.setdefault("cur", {})
        self.next_id = max([p["id"] for p in self.points] + [0]) + 1

    def save(self):
        tmp = DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rects": self.rects, "points": [p for p in self.points if p["scans"]]}, f, ensure_ascii=False)
        os.replace(tmp, DATA)

    def rebuild(self):
        self.model = locate.Model(self.rects, self.points)
        self.filter = locate.Filter(self.model) if self.model.ready else None
        self.est = None
        self.last_update = None
        self.rev += 1

    def point(self, pid):
        for p in self.points:
            if p["id"] == pid:
                return p
        return None

    # ---- tarayıcıdan gelen ölçümler ---------------------------------------
    def on_message(self, msg):
        with self.lock:
            kind = msg.get("type")
            if kind == "error":
                self.err = msg.get("error")
            elif kind == "scan":
                self.on_scan(msg)
            elif kind == "cur":
                self.on_cur(msg)

    def on_scan(self, msg):
        nets, named = normalize(msg.get("nets") or [])
        if not nets:
            return
        self.err = None
        self.no_names = not named
        if self.scan:
            gap = msg["t"] - self.scan["t"]
            if 5 < gap < 40:
                self.period = 0.6 * self.period + 0.4 * gap
        self.scan_dur = msg["t"] - msg["t0"]
        self.scan = {"t": msg["t"], "nets": nets}
        c = self.collecting
        if not c:
            self.observe(nets, msg["t"], 0.6)
            return
        if msg["t0"] < c["since"] - 0.3:
            return  # tıklamadan önce başlamış tarama: o sırada kullanıcı yürüyor olabilir
        if c["left"] > 0:
            self.point(c["id"])["scans"].append(nets)
            c["left"] -= 1
            if c["left"] == 0:
                c["done_at"] = time.time()
        self.maybe_finish()

    def maybe_finish(self):
        """Taramalar bittikten sonra, yeterli anlık okuma da toplanınca ölçümü kapatır."""
        c = self.collecting
        if not c or c["left"] > 0:
            return
        now = time.time()
        samples = max((len(v) for v in self.point(c["id"])["cur"].values()), default=0)
        live_ok = self.cur and now - self.cur["t"] < 6  # bir ağa bağlı değilse anlık okuma hiç gelmez
        if samples >= MIN_LIVE or not live_ok or now - c["done_at"] > 8:
            self.collecting = None
            self.save()
            self.rebuild()

    def on_cur(self, msg):
        self.err = None
        key = f"{locate.LIVE} bağlı ağ (anlık) · kanal {msg.get('ch') or 0}"
        rssi = round(msg["rssi"], 1)
        self.cur = {"t": msg["t"], "key": key, "rssi": rssi}
        if msg.get("samples"):
            self.motion.add(msg.get("t0", msg["t"] - 0.5), msg["t"], msg["samples"])
        if self.motion.on and time.time() - self.motion.last_poll > 6:
            self.set_motion(False)  # Hareket sekmesi kapanmış: taramayı yeniden başlat
        c = self.collecting
        if c:
            if msg["t"] >= c["since"] + 0.6:
                self.point(c["id"])["cur"].setdefault(key, []).append(rssi)
            self.maybe_finish()
        else:
            # Art arda gelen anlık okumalar birbirine çok benzer; aşırı güvenmemek için ağırlığı düşük.
            self.observe({key: rssi}, msg["t"], 0.1)

    def send(self, cmd):
        """Tarayıcı sürecine komut yollar ("scan on" / "scan off")."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    def set_motion(self, on):
        with self.lock:
            if on and self.collecting:
                raise Bad("Ölçüm sürerken hareket modu açılamaz.")
            self.motion.on = bool(on)
            self.motion.last_poll = time.time()
            self.send("scan off" if on else "scan on")

    def motion_state(self):
        with self.lock:
            self.motion.last_poll = time.time()
            return self.motion.snapshot()

    def motion_learn(self):
        with self.lock:
            self.motion.learn()

    def motion_sens(self, body):
        if body.get("sens") not in (1, 2, 3):
            raise Bad("Geçersiz hassasiyet.")
        with self.lock:
            self.motion.sens = body["sens"]

    def observe(self, obs, t, temper):
        if not self.filter or self.motion.on:  # hareket modunda MacBook sabit: konum güncellenmez
            return
        dt = min(max(t - self.last_update, 0.05), 30.0) if self.last_update else 1.0
        self.est = self.filter.update(obs, dt, temper)
        self.last_update = t

    # ---- arayüz istekleri --------------------------------------------------
    def state(self):
        now = time.time()
        with self.lock:
            self.maybe_finish()
            c = self.collecting
            cp = self.point(c["id"]) if c else None
            return {
                "rev": self.rev,
                "rects": self.rects,
                "grid": self.model.grid(),
                "points": [{"id": p["id"], "x": p["x"], "y": p["y"], "n": len(p["scans"])} for p in self.points],
                "nets": sorted(self.scan["nets"].items(), key=lambda kv: -kv[1]) if self.scan else [],
                "scan_age": round(now - self.scan["t"], 1) if self.scan else None,
                "scan_paused": self.motion.on,
                "next_scan": round(self.scan["t"] + self.period - self.scan_dur - now, 1) if self.scan else None,
                "period": round(self.period, 1),
                "cur": {"key": self.cur["key"], "rssi": self.cur["rssi"], "age": round(now - self.cur["t"], 1)} if self.cur else None,
                "collecting": {"id": c["id"], "x": cp["x"], "y": cp["y"], "left": c["left"], "total": c["total"]} if c else None,
                "est": self.est,
                "keys": self.model.keys,
                "ready": self.model.ready,
                "warn": self.err or ("no_names" if self.no_names else None),
            }

    def heat(self, key):
        with self.lock:
            return self.model.heat(key) or {"key": key, "vals": None}

    def set_plan(self, body):
        rects = body.get("rects")
        if not isinstance(rects, list) or not 1 <= len(rects) <= 12:
            raise Bad("Plan 1 ile 12 arasında alan içermeli.")
        clean = []
        for r in rects:
            try:
                name = str(r.get("name") or "").strip()[:30] or "Alan"
                x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
            except (AttributeError, KeyError, TypeError, ValueError):
                raise Bad("Alan ölçüleri sayı olmalı.")
            if not (0.3 <= w <= 30 and 0.3 <= h <= 30 and -30 <= x <= 30 and -30 <= y <= 30):
                raise Bad(f"“{name}” için ölçüler geçersiz (genişlik ve uzunluk 0,3–30 m olmalı).")
            clean.append({"name": name, "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)})
        with self.lock:
            if self.collecting:
                raise Bad("Ölçüm sürerken plan değiştirilemez.")
            self.rects = clean
            self.save()
            self.rebuild()

    def calibrate(self, body):
        try:
            n = min(max(int(body.get("n", 2)), 1), 6)
        except (TypeError, ValueError):
            raise Bad("Geçersiz tarama sayısı.")
        with self.lock:
            if self.collecting:
                raise Bad("Bir ölçüm zaten sürüyor.")
            if self.motion.on:
                self.set_motion(False)  # ölçüm için tam tarama gerekiyor
            if "id" in body:
                p = self.point(body["id"])
                if not p:
                    raise Bad("Nokta bulunamadı.")
            else:
                try:
                    x, y = float(body["x"]), float(body["y"])
                except (KeyError, TypeError, ValueError):
                    raise Bad("Geçersiz konum.")
                if locate.zone_at(self.rects, x, y) < 0:
                    raise Bad("Bu nokta planın dışında.")
                p = {"id": self.next_id, "x": round(x, 2), "y": round(y, 2), "scans": [], "cur": {}}
                self.next_id += 1
                self.points.append(p)
            self.collecting = {"id": p["id"], "since": time.time(), "left": n, "total": n}

    def cancel(self):
        with self.lock:
            if not self.collecting:
                return
            self.collecting = None
            self.points = [p for p in self.points if p["scans"]]
            self.save()
            self.rebuild()

    def delete(self, body):
        with self.lock:
            if self.collecting:
                raise Bad("Ölçüm sürerken nokta silinemez.")
            self.points = [p for p in self.points if p["id"] != body.get("id")]
            self.save()
            self.rebuild()

    def clear(self):
        with self.lock:
            self.collecting = None
            self.points = []
            self.save()
            self.rebuild()


def scanner_loop(app):
    script = os.path.join(HERE, "wifiscan.swift")
    while True:
        swift = shutil.which("swift")
        if not swift:
            app.on_message({"type": "error", "error": "no_swift"})
            time.sleep(10)
            continue
        # Derlenmiş ikili dosya ağ adlarını okuyamadığı için betik yorumlayıcıyla çalıştırılıyor.
        proc = subprocess.Popen([swift, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        app.proc = proc
        if app.motion.on:
            app.send("scan off")
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                app.on_message(msg)
            except Exception as e:  # tek bir bozuk ölçüm tarayıcıyı durdurmasın
                print("ölçüm işlenemedi:", e, file=sys.stderr)
        proc.wait()
        app.on_message({"type": "error", "error": "scanner_stopped"})
        time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        if "/api/state" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1"):
            self.send_json({"error": "Yalnızca bu bilgisayardan erişilebilir."}, 403)
            return False
        return True

    def do_HEAD(self):
        self.send_response(200 if urlparse(self.path).path == "/" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.local_only():
            return
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/api/state":
            self.send_json(self.app.state())
        elif url.path == "/api/motion":
            self.send_json(self.app.motion_state())
        elif url.path == "/api/heat":
            key = (parse_qs(url.query).get("key") or [""])[0]
            self.send_json(self.app.heat(key))
        else:
            self.send_json({"error": "Bulunamadı."}, 404)

    def do_POST(self):
        if not self.local_only():
            return
        # JSON içerik türü şartı, başka sitelerin bu sunucuya gizlice istek atmasını engeller.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json({"error": "JSON bekleniyor."}, 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json({"error": "Geçersiz istek."}, 400)
        routes = {
            "/api/plan": lambda: self.app.set_plan(body),
            "/api/calibrate": lambda: self.app.calibrate(body),
            "/api/cancel": self.app.cancel,
            "/api/delete": lambda: self.app.delete(body),
            "/api/clear": self.app.clear,
            "/api/motion": lambda: self.app.set_motion(body.get("on")),
            "/api/motion/learn": self.app.motion_learn,
            "/api/motion/sens": lambda: self.app.motion_sens(body),
        }
        action = routes.get(urlparse(self.path).path)
        if not action:
            return self.send_json({"error": "Bulunamadı."}, 404)
        try:
            action()
        except Bad as e:
            return self.send_json({"error": str(e)}, 400)
        self.send_json({"ok": True})


def main():
    app = App()
    Handler.app = app
    threading.Thread(target=scanner_loop, args=(app,), daemon=True).start()

    def stop_scanner():
        if app.proc and app.proc.poll() is None:
            app.proc.terminate()

    atexit.register(stop_scanner)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"WiFi Konum çalışıyor: http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
