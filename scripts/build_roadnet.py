"""Main roads of Bangkok (OpenStreetMap) for the flood map, so roads expected to flood can be drawn.

Run once (needs internet; update.py runs it automatically when data/road_net.json is missing).
Source: Overpass API, highway = motorway / trunk / primary / secondary / tertiary (+ _link) inside the BMA box
Output: data/road_net.json  {"ways": [[name, class, [[lon, lat], ...]], ...]}   (points about 60 m apart)
Only the Python standard library is needed.
"""
import json, math, os, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574
BOX = (13.49, 100.32, 13.96, 100.95)                 # south, west, north, east
CLS = {'motorway': 1, 'trunk': 1, 'primary': 2, 'secondary': 3, 'tertiary': 4}
STEP = 60                                            # m between kept points


def thin(g):
    out = [g[0]]; acc = 0.0
    for a, b in zip(g, g[1:]):
        acc += math.hypot((b[0] - a[0]) * KX, (b[1] - a[1]) * KY)
        if acc >= STEP: out.append(b); acc = 0.0
    if out[-1] != g[-1]: out.append(g[-1])
    return out


def convert(elements):
    ways = []
    for e in elements:
        if e.get('type') != 'way' or not e.get('geometry'): continue
        t = e.get('tags') or {}; hw = (t.get('highway') or '').replace('_link', '')
        if hw not in CLS: continue
        g = [[round(p['lon'], 5), round(p['lat'], 5)] for p in e['geometry']]
        ways.append([t.get('name') or t.get('name:th') or t.get('ref') or '', CLS[hw], thin(g)])
    return dict(source='OpenStreetMap (Overpass API)', ways=ways)


def main():
    s, w, n, e = BOX
    q = f'[out:json][timeout:240];way["highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]({s},{w},{n},{e});out tags geom;'
    req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': q}).encode(),
                                 headers={'User-Agent': 'FloodBKK research prototype'})
    with urllib.request.urlopen(req, timeout=300) as r:
        el = json.loads(r.read().decode('utf-8'))['elements']
    out = convert(el)
    json.dump(out, open(P('road_net.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f"road_net.json: {len(out['ways'])} roads, {os.path.getsize(P('road_net.json')) // 1024} KB")


if __name__ == '__main__':
    main()
