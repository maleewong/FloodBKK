"""Where the water goes after it leaves Bangkok: follow each boundary outlet along OSM waterways outside BKK
(Samut Prakan, Chachoengsao) to the sea or a big river. Display only; the drainage model still stops at the outlet.

Input : data/osm_outer.json (fetched once from Overpass, see NOTES), data/model_data.json (outlets)
Output: data/outer_paths.json  {paths:[{outlet, end, via, km, xy}], bg:[[x,y,...] ...]}
Needs numpy, scipy, shapely.
"""
import json, os, math, heapq, collections
import numpy as np
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574

O = json.load(open(P('osm_outer.json'), encoding='utf-8')); S = O['scale']
M = json.load(open(P('model_data.json'), encoding='utf-8'))
N = M['net']['nodes']; OU = M['net']['outlets']
RIVERS = ('แม่น้ำเจ้าพระยา', 'แม่น้ำบางปะกง', 'แม่น้ำท่าจีน')
# the sea: water cells of the DEM mask connected to its southern edge (Gulf of Thailand)
import base64
from scipy import ndimage
DM = json.load(open(P('dem.json')))
_wm = np.unpackbits(np.frombuffer(base64.b64decode(DM['water']), dtype=np.uint8))[:DM['w'] * DM['h']].reshape(DM['h'], DM['w']).astype(bool)
_wm = ndimage.binary_opening(_wm, iterations=4)        # keep wide open water only (drops the Chao Phraya channel)
_lab, _n = ndimage.label(_wm); _sz = ndimage.sum(_wm, _lab, range(1, _n + 1))
SEA = np.isin(_lab, [b for b in set(_lab[-3:].ravel()) if b and _sz[b - 1] > 500])
_sj, _si = np.nonzero(SEA)
SEA_XY = np.c_[(DM['lon0'] + (_si + 0.5) * DM['cell']) * math.cos(math.radians(13.75)) * 111320, (DM['lat0'] - (_sj + 0.5) * DM['cell']) * 110574]
SEA_LL = np.c_[DM['lon0'] + (_si + 0.5) * DM['cell'], DM['lat0'] - (_sj + 0.5) * DM['cell']]

# vertices
V, vname, edges = [], [], []
for name, kind, coords in O['ways']:
    prev = None
    for x, y in coords:
        k = len(V); V.append((x / S, y / S)); vname.append(name)
        if prev is not None: edges.append((prev, k))
        prev = k
V = np.array(V); XY = np.c_[V[:, 0] * KX, V[:, 1] * KY]
adj = collections.defaultdict(list)
def link(a, b, extra=0.0):
    d = float(np.hypot(*(XY[a] - XY[b]))) + extra
    adj[a].append((b, d)); adj[b].append((a, d))
for a, b in edges: link(a, b)
# ways meet at shared OSM nodes; points were thinned, so join vertices of different ways within 60 m
tree = cKDTree(XY)
for a, b in tree.query_pairs(60):
    link(a, b, 20.0)

# routes must stay outside Bangkok (except the first ~1 km next to the outlet)
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from shapely.prepared import prep
def polys(c):
    return c if isinstance(c[0][0][0], (list, tuple)) else [c]
BKK = prep(unary_union([shape(dict(type='MultiPolygon', coordinates=polys(g[2]))).buffer(0) for g in M['geo']]).buffer(-0.002))
inside = np.array([BKK.contains(Point(x, y)) for x, y in V])
sea_tree = cKDTree(SEA_XY); sea_d, sea_i = sea_tree.query(XY)
target = {}
for i, (x, y) in enumerate(V):
    if inside[i]: continue
    if vname[i] in RIVERS: target[i] = vname[i]
    elif sea_d[i] < 400: target[i] = 'ทะเล (อ่าวไทย)'

def route(start, own):
    dist = {start: 0.0}; prev = {}; h = [(0.0, start)]
    while h:
        d, u = heapq.heappop(h)
        if d > dist.get(u, 1e18): continue
        if u in target and d > 0: break
        for v, w in adj[u]:
            if inside[v] and np.hypot(*(XY[v] - XY[start])) > 1000: continue
            w2 = w * (0.6 if own and vname[v] == own else 1.0)     # prefer to keep following the same canal
            nd = d + w2
            if nd < dist.get(v, 1e18): dist[v] = nd; prev[v] = u; heapq.heappush(h, (nd, v))
    else:
        return None
    path = [u]
    while path[-1] in prev: path.append(prev[path[-1]])
    return path[::-1], target[u]

paths = []
for o in OU:
    lon, lat = N[o['node']][:2]
    if lon < 100.47: continue                          # west side (Samut Sakhon) is outside the fetched area
    own = o['name'].replace('ทางออกขอบเขต ', '')
    d0, i0 = tree.query((lon * KX, lat * KY))
    if d0 > 800: continue
    r = route(int(i0), own)
    if not r: continue
    pth, end = r
    L = sum(float(np.hypot(*(XY[a] - XY[b]))) for a, b in zip(pth, pth[1:]))
    via = []
    for k in pth:
        if vname[k] and (not via or via[-1] != vname[k]) and vname[k] not in via: via.append(vname[k])
    coords = [[round(lon, 5), round(lat, 5)]] + [[round(V[k][0], 5), round(V[k][1], 5)] for k in pth]
    if end.startswith('ทะเล'):                              # last few hundred metres to the shoreline
        q = SEA_LL[sea_i[pth[-1]]]; coords.append([round(float(q[0]), 5), round(float(q[1]), 5)])
    paths.append(dict(outlet=o['name'], node=o['node'], end=end, via=via[:6], km=round(L / 1000, 1), xy=coords))
    print(f"{own:22s} -> {end:18s} {L/1000:5.1f} km via {' > '.join(via[:5])}")

# faint background of outer canals inside the DEM frame (thin, rounded)
bg = []
for name, kind, coords in O['ways']:
    c = [(x / S, y / S) for x, y in coords]
    if not any(y < 13.74 or x > 100.83 for x, y in c): continue
    c = c[::3] + ([c[-1]] if len(c) % 3 != 1 else [])
    if len(c) < 2: continue
    bg.append([v for x, y in c for v in (round(x, 4), round(y, 4))])
out = dict(source=O['source'], note='ทางน้ำนอกเขต กทม. ใช้แสดงทิศทางเท่านั้น ไม่ได้อยู่ในโมเดล', paths=paths, bg=bg)
json.dump(out, open(P('outer_paths.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('paths', len(paths), 'bg ways', len(bg), 'bytes', os.path.getsize(P('outer_paths.json')))
