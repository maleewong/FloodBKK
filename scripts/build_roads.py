"""Road segment (about 300 m) under each road-flood sensor, so the overview map can colour the road instead of a symbol.

Run once (needs internet; update.py runs it automatically when data/road_segments.json is missing).
Input : data/road_sensor.csv (code, road, lat, lon)
Source: OpenStreetMap highways within 80 m of each sensor (Overpass API)
Output: data/road_segments.json  {code: [osm_road_name, [[lon, lat], ...]]}
Only the Python standard library is needed.
"""
import csv, json, math, os, re, sys, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574
R = 150                                   # metres on each side of the sensor
HW = '^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service)(_link)?$'


def norm(s):
    return re.sub(r'\s+', '', re.sub(r'^(ถนน|ถ\.|ซอย|ซ\.)', '', s or ''))[:6]


def segments(sensors, ways):
    out = {}
    for code, la, lo, rd in sensors:
        best = None
        for w in ways:
            g = w['geometry']; nm = (w.get('tags') or {}).get('name', '')
            bonus = 40 if rd and nm and norm(nm) and (norm(nm) in norm(rd) or norm(rd) in norm(nm)) else 0
            for i in range(len(g) - 1):
                ax, ay = (g[i]['lon'] - lo) * KX, (g[i]['lat'] - la) * KY
                bx, by = (g[i + 1]['lon'] - lo) * KX, (g[i + 1]['lat'] - la) * KY
                dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy or 1
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
                d = math.hypot(ax + t * dx, ay + t * dy) - bonus
                if best is None or d < best[0]: best = (d, w, i, t)
        if best is None or best[0] > 80: continue
        _, w, i, t = best
        g = [((p['lon'] - lo) * KX, (p['lat'] - la) * KY) for p in w['geometry']]
        p0 = (g[i][0] + t * (g[i + 1][0] - g[i][0]), g[i][1] + t * (g[i + 1][1] - g[i][1]))

        def walk(step):
            res, acc, prev, k = [], 0.0, p0, (i + 1 if step > 0 else i)
            while 0 <= k < len(g):
                q = g[k]; s = math.hypot(q[0] - prev[0], q[1] - prev[1])
                if acc + s >= R:
                    f = (R - acc) / s if s else 0; res.append((prev[0] + f * (q[0] - prev[0]), prev[1] + f * (q[1] - prev[1]))); break
                acc += s; res.append(q); prev = q; k += step
            return res
        seg = walk(-1)[::-1] + [p0] + walk(1)
        out[code] = [(w.get('tags') or {}).get('name', ''), [[round(lo + x / KX, 5), round(la + y / KY, 5)] for x, y in seg]]
    return out


def main():
    sensors = []
    for r in csv.DictReader(open(P('road_sensor.csv'), encoding='utf-8-sig')):
        try: sensors.append((r['code'], float(r['lat']), float(r['lon']), r.get('road') or ''))
        except (TypeError, ValueError): pass
    q = '[out:json][timeout:180];(' + ''.join(f'way(around:80,{a},{b})[highway~"{HW}"];' for _, a, b, _ in sensors) + ');out geom;'
    req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': q}).encode(),
                                 headers={'User-Agent': 'FloodBKK research prototype'})
    with urllib.request.urlopen(req, timeout=240) as r:
        ways = [e for e in json.loads(r.read().decode('utf-8'))['elements'] if e.get('type') == 'way' and e.get('geometry')]
    out = segments(sensors, ways)
    json.dump(out, open(P('road_segments.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f'road segments: {len(out)} of {len(sensors)} sensors ({len(ways)} OSM ways)')


if __name__ == '__main__':
    main()
