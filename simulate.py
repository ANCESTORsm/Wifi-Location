"""Algoritmayı sahte bir stüdyo dairede dener (gerçek ev değil; yalnızca kodun doğru çalıştığını gösterir).

Çalıştırma: python3 simulate.py
"""
import math
import random
import statistics

import locate

RECTS = [
    {"name": "Oda", "x": 0, "y": 0, "w": 4.5, "h": 3.6},
    {"name": "Tuvalet", "x": 4.5, "y": 0, "w": 1.2, "h": 1.5},
]
# (ad, x, y, 1 metredeki güç, yol kaybı üssü, tuvalet duvarı kaybı). İlk ikisi odadaki modem, diğerleri komşular.
APS = [
    ("modem 5G", 0.4, 3.2, -38, 2.4, 6),
    ("modem 2.4G", 0.4, 3.2, -35, 2.2, 4),
    ("komşu A", -3.0, 1.0, -42, 3.0, 5),
    ("komşu B", 2.5, -4.0, -40, 3.0, 3),
    ("komşu C", 8.0, 2.5, -40, 3.0, -2),
    ("komşu D", 2.0, 7.5, -40, 3.2, 5),
]
CUR = "~bağlı"


def make_world(rng, noise, drift):
    phases = {a[0]: [rng.uniform(0, 6.28) for _ in range(4)] for a in APS}

    def true_rssi(ap, x, y):
        name, ax, ay, p0, n, wall = ap
        d = max(0.3, math.hypot(x - ax, y - ay))
        v = p0 - 10 * n * math.log10(d)
        if locate.zone_at(RECTS, x, y) == 1:
            v -= wall
        ph = phases[name]  # konuma bağlı sabit sapma (yansımalar, mobilya)
        v += 3.0 * math.sin(1.7 * x + ph[0]) * math.sin(1.3 * y + ph[1]) + 1.5 * math.sin(3.1 * x + ph[2]) * math.sin(2.7 * y + ph[3])
        return v

    def session():
        """Bir duruş boyunca sabit kalan sapma: vücudun sinyali kesmesi, laptopun yönü, yavaş dalgalanma."""
        return {a[0]: rng.gauss(0, drift) for a in APS}

    def scan(x, y, b):
        out = {}
        for ap in APS:
            v = true_rssi(ap, x, y) + b[ap[0]] + rng.gauss(0, noise)
            if v > -90 and rng.random() > 0.05:
                out[ap[0]] = round(v)
        return out

    def cur(x, y, b):
        return {CUR: round(true_rssi(APS[0], x, y) + b[APS[0][0]] + rng.gauss(0, noise * 0.6), 1)}

    return scan, cur, session


def run(noise, drift, scans_per_point, spacing, t_scan=0.6, t_cur=0.1, seed=1, trials=120):
    rng = random.Random(seed)
    scan, cur, session = make_world(rng, noise, drift)
    points = []
    y = spacing / 2
    while y < 3.6:
        x = spacing / 2
        while x < 4.5:
            points.append((x, y))
            x += spacing
        y += spacing
    points += [(5.1, 0.5), (5.1, 1.1)]
    cal = []
    for i, (x, y) in enumerate(points):
        b = session()
        p = {"id": i, "x": x, "y": y, "scans": [scan(x, y, b) for _ in range(scans_per_point)], "cur": {CUR: []}}
        for _ in range(13 * scans_per_point):
            p["cur"][CUR].append(cur(x, y, b)[CUR])
        cal.append(p)
    model = locate.Model(RECTS, cal)

    err1, err3, zone_ok = [], [], 0
    for _ in range(trials):
        if rng.random() < 0.15:
            x, y = rng.uniform(4.6, 5.6), rng.uniform(0.1, 1.4)
        else:
            x, y = rng.uniform(0.2, 4.3), rng.uniform(0.2, 3.4)
        b = session()
        f = locate.Filter(model)
        est = None
        for k in range(3):  # 3 tam tarama (~40 sn), aralarında yarım saniyede bir anlık okuma
            est = f.update(scan(x, y, b), dt=0.55, temper=t_scan)
            for _ in range(24):
                est = f.update(cur(x, y, b), dt=0.55, temper=t_cur)
            if k == 0:
                err1.append(math.hypot(est["x"] - x, est["y"] - y))
        err3.append(math.hypot(est["x"] - x, est["y"] - y))
        zone_ok += est["zone"] == locate.zone_at(RECTS, x, y)
    return len(points), statistics.median(err1), statistics.median(err3), sorted(err3)[int(0.9 * trials)], zone_ok / trials


if __name__ == "__main__":
    import sys

    if "--tune" in sys.argv:
        print("t_scan t_cur   hata@13sn  hata@40sn  %90 hata  oda")
        for t_scan, t_cur in ((0.6, 0.3), (0.6, 0.1), (0.6, 0.03), (0.3, 0.1), (1.0, 0.1)):
            n, e1, e3, e90, z = run(3.0, 2.5, 2, 1.0, t_scan, t_cur)
            print(f"{t_scan:5.2f} {t_cur:5.2f}   {e1:7.2f} m  {e3:7.2f} m  {e90:6.2f} m  {z * 100:4.0f}%")
        sys.exit()
    print("gürültü  sapma  tarama/nokta  aralık  nokta   hata@13sn  hata@40sn  %90 hata  oda doğruluğu")
    for noise, drift in ((2.5, 0.0), (3.0, 2.5), (4.0, 4.0)):
        for spp, spacing in ((1, 1.2), (2, 1.0), (3, 0.8)):
            n, e1, e3, e90, z = run(noise, drift, spp, spacing)
            print(f"{noise:4.1f} dB  {drift:3.1f} dB  {spp:^12d}  {spacing:4.1f} m  {n:4d}   {e1:7.2f} m  {e3:7.2f} m  {e90:6.2f} m  {z * 100:8.0f}%")
