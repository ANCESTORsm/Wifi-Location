"""WiFi parmak izinden konum tahmini.

Model: ölçüm noktalarındaki ortalama sinyalleri plan üzerindeki bir ızgaraya yayar (sinyal haritası).
Filter: her yeni ölçümde "hangi hücredeyim" olasılığını günceller (ızgara tabanlı Bayes filtresi).
"""
import math

FLOOR = -95.0      # duyulmayan ağ için varsayılan sinyal (dBm)
LIVE = "~"         # bağlı ağın anlık okumasından gelen anahtarların ön eki
CELL = 0.2         # ızgara hücresi (m)
WALL = 2.0         # farklı odadaki ölçüm noktasına eklenen sanal mesafe (m)
IDW_POWER = 3.0    # sinyal haritasında uzak noktaların etkisi ne kadar hızlı azalsın
MOVE = 0.3         # beklenen hareket (m / sqrt(sn))
UNIFORM = 0.02     # her adımda "her yerde olabilirim" payı; filtrenin takılıp kalmasını önler


def zone_at(rects, x, y):
    for i, r in enumerate(rects):
        if r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]:
            return i
    return -1


def point_values(p):
    """Bir ölçüm noktasında her ağ için okunan değerler: {ağ: [dBm, ...]}"""
    scans = p.get("scans") or []
    vals = {}
    for s in scans:
        for k, v in s.items():
            vals.setdefault(k, []).append(v)
    # Taramaların en az yarısında duyulan ağlar güvenilir sayılır.
    out = {k: v for k, v in vals.items() if len(v) * 2 >= len(scans)}
    for k, v in (p.get("cur") or {}).items():
        if len(v) >= 3:
            out[k] = v
    return out


class Model:
    def __init__(self, rects, points):
        self.rects = rects
        if rects:
            x0 = min(r["x"] for r in rects)
            y0 = min(r["y"] for r in rects)
            x1 = max(r["x"] + r["w"] for r in rects)
            y1 = max(r["y"] + r["h"] for r in rects)
        else:
            x0 = y0 = 0.0
            x1 = y1 = 1.0
        self.x0, self.y0 = x0, y0
        self.nx = max(1, int(math.ceil((x1 - x0) / CELL - 1e-9)))
        self.ny = max(1, int(math.ceil((y1 - y0) / CELL - 1e-9)))
        n = self.nx * self.ny
        self.cx = [x0 + (i % self.nx + 0.5) * CELL for i in range(n)]
        self.cy = [y0 + (i // self.nx + 0.5) * CELL for i in range(n)]
        self.zone = [zone_at(rects, self.cx[i], self.cy[i]) for i in range(n)]
        self.valid = [i for i in range(n) if self.zone[i] >= 0]

        self.pts = [p for p in points if p.get("scans")]
        self.means = []
        var_num, var_den = {}, {}
        for p in self.pts:
            means = {}
            for k, vs in point_values(p).items():
                m = sum(vs) / len(vs)
                means[k] = m
                if len(vs) >= 2:
                    var_num[k] = var_num.get(k, 0.0) + sum((v - m) ** 2 for v in vs)
                    var_den[k] = var_den.get(k, 0) + len(vs) - 1
            self.means.append(means)
        keys = {k for m in self.means for k in m}
        self.keys = sorted(keys, key=lambda k: -max(m.get(k, FLOOR) for m in self.means))
        # Ağ başına gürültü: aynı noktadaki okumaların varyansı + harita hatası payı.
        self.s2 = {}
        for k in self.keys:
            v = var_num[k] / var_den[k] if var_den.get(k) else 9.0
            self.s2[k] = min(max(v, 4.0), 64.0) + 6.0
        self.ready = len(self.pts) >= 2 and bool(self.keys)
        self.mu = {}
        if self.pts and self.keys:
            self._interpolate()

    def _interpolate(self):
        n = self.nx * self.ny
        pz = [zone_at(self.rects, p["x"], p["y"]) for p in self.pts]
        mu = {k: [None] * n for k in self.keys}
        half = IDW_POWER / 2.0
        for i in self.valid:
            ws = []
            for j, p in enumerate(self.pts):
                d = math.hypot(self.cx[i] - p["x"], self.cy[i] - p["y"])
                if pz[j] != self.zone[i]:
                    d += WALL
                ws.append(1.0 / (d * d + 0.04) ** half)
            for k in self.keys:
                live = k.startswith(LIVE)
                acc = tot = 0.0
                for j, w in enumerate(ws):
                    v = self.means[j].get(k)
                    if v is None:
                        if live:
                            continue  # bu noktada anlık okuma alınmamış: "duyulmadı" değil, "veri yok"
                        v = FLOOR  # taramada görünmeyen ağ gerçekten duyulmuyor demek
                    acc += w * v
                    tot += w
                mu[k][i] = acc / tot if tot else FLOOR
        self.mu = mu

    def grid(self):
        return {"x0": self.x0, "y0": self.y0, "nx": self.nx, "ny": self.ny, "cell": CELL}

    def heat(self, key):
        vals = self.mu.get(key)
        if not vals:
            return None
        present = [v for v in vals if v is not None]
        return {
            "key": key,
            "vals": [None if v is None else round(v, 1) for v in vals],
            "min": round(min(present), 1),
            "max": round(max(present), 1),
        }


class Filter:
    def __init__(self, model):
        self.m = model
        n = model.nx * model.ny
        self.p = [0.0] * n
        for i in model.valid:
            self.p[i] = 1.0 / len(model.valid)

    def _predict(self, dt):
        """Hareket modeli: olasılığı geçen süreyle orantılı biçimde yay."""
        m = self.m
        nx, ny, n = m.nx, m.ny, m.nx * m.ny
        sigma = max(MOVE * math.sqrt(max(dt, 0.05)) / CELL, 0.3)
        r = max(1, int(math.ceil(3 * sigma)))
        kern = [math.exp(-0.5 * (d / sigma) ** 2) for d in range(-r, r + 1)]
        src = self.p
        tmp = [0.0] * n
        for y in range(ny):
            base = y * nx
            for x in range(nx):
                acc = 0.0
                lo, hi = max(0, x - r), min(nx - 1, x + r)
                for xx in range(lo, hi + 1):
                    acc += src[base + xx] * kern[xx - x + r]
                tmp[base + x] = acc
        out = [0.0] * n
        for i in m.valid:
            x, y = i % nx, i // nx
            acc = 0.0
            lo, hi = max(0, y - r), min(ny - 1, y + r)
            for yy in range(lo, hi + 1):
                acc += tmp[yy * nx + x] * kern[yy - y + r]
            out[i] = acc
        tot = sum(out) or 1.0
        u = UNIFORM / len(m.valid)
        for i in m.valid:
            out[i] = (1 - UNIFORM) * out[i] / tot + u
        return out

    def update(self, obs, dt=1.0, temper=0.6):
        """obs: {ağ: dBm}. temper < 1: ölçümler birbirinden tam bağımsız olmadığı için güveni kısar."""
        m = self.m
        prior = self._predict(dt)
        keys = [k for k in obs if k in m.s2]
        if not keys:
            self.p = prior
            return self.estimate()
        ll = {}
        best = -1e30
        for i in m.valid:
            s = 0.0
            for k in keys:
                d = obs[k] - m.mu[k][i]
                s += d * d / m.s2[k]
            v = -0.5 * temper * s
            ll[i] = v
            if v > best:
                best = v
        post = [0.0] * len(prior)
        tot = 0.0
        for i in m.valid:
            v = prior[i] * math.exp(ll[i] - best)
            post[i] = v
            tot += v
        if tot <= 0:
            self.p = prior
        else:
            self.p = [v / tot for v in post]
        return self.estimate()

    def estimate(self):
        m, p = self.m, self.p
        zp = [0.0] * len(m.rects)
        for i in m.valid:
            zp[m.zone[i]] += p[i]
        top = max(range(len(zp)), key=lambda z: zp[z])
        # Nokta duvarın içine düşmesin diye ortalamayı en olası odanın içinde al.
        mx = sum(p[i] * m.cx[i] for i in m.valid if m.zone[i] == top) / zp[top]
        my = sum(p[i] * m.cy[i] for i in m.valid if m.zone[i] == top) / zp[top]
        var = sum(p[i] * ((m.cx[i] - mx) ** 2 + (m.cy[i] - my) ** 2) for i in m.valid)
        pmax = max(p) or 1.0
        return {
            "x": round(mx, 2),
            "y": round(my, 2),
            "spread": round(math.sqrt(var), 2),
            "zone": top,
            "zones": [{"name": r["name"], "p": round(zp[z], 3)} for z, r in enumerate(m.rects)],
            "post": [(-1 if m.zone[i] < 0 else int(round(p[i] / pmax * 100))) for i in range(len(p))],
        }
