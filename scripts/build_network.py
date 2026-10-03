"""Build the drainage network model inputs (one-off; re-run only when the canal map or assumptions change).

Inputs  (data/):  osm_net.json        canal/river graph from OpenStreetMap (Overpass, (c) OSM contributors, ODbL)
                  canal_station.csv   BMA water-level stations (location, canal name, warning/critical levels)
                  pump_daily.csv      BMA telemetry pump stations (daily capacity m3/day)
                  pump_report2557.json  riverside pump stations + capacity (m3/s) from แผนปฏิบัติการฯ 2557 (Report_Book)
                  bkk_districts.geojson
Output  (data/):  network.json        nodes, edges, sinks, catchment areas, station links -> used by render.py
Needs: numpy, scipy, shapely, networkx  (pip install numpy scipy shapely networkx)
"""
import json, csv, os, re, math
from collections import defaultdict, Counter
import numpy as np, networkx as nx
from scipy.spatial import cKDTree
from shapely.geometry import shape, Point, LineString
from shapely.ops import unary_union
from shapely.prepared import prep

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)

# ---------------- assumptions (documented on the model page) ----------------
CLASS = {  # width m, depth m  -> capacity = v * w * d  (v set in the page, default 0.5 m/s)
    'A': dict(w=20, d=2.5, label='คลองหลัก (ชื่อคลอง ยาวรวม > 8 กม. หรือกว้าง ≥ 15 ม.)'),
    'B': dict(w=10, d=2.0, label='คลองรองที่มีชื่อ'),
    'C': dict(w=5, d=1.5, label='คลองซอย/ลำราง ไม่มีชื่อ'),
}
CP_NAMES = {'แม่น้ำเจ้าพระยา'}
INTERNAL_PUMPS = {'WL.SSB.04', 'WL.SSN.08', 'WL.CKS.01'}   # transfer between canals, not out of the system
GRID_M = 300                                                # catchment grid spacing (m)
SEA_LAT = 13.70                                             # boundary outlets only south of this latitude

KX = math.cos(math.radians(13.75)) * 111320; KY = 110574    # deg -> m (local)
def xy(lon, lat): return (lon * KX, lat * KY)

net = json.load(open(P('osm_net.json')))
names = net['names']
nodes = [(x / 1e5, y / 1e5) for x, y in net['nodes']]
geo = json.load(open(P('bkk_districts.geojson')))
dpolys = {int(f['properties']['code'][2:]): shape(f['geometry']) for f in geo['features']}
dnames = {int(f['properties']['code'][2:]): f['properties']['name'] for f in geo['features']}
bkk = unary_union(list(dpolys.values())); bkk_p = prep(bkk); bkk_buf = prep(bkk.buffer(0.004))
dprep = {k: prep(v) for k, v in dpolys.items()}

def district_of(lon, lat):
    pt = Point(lon, lat)
    for k, p in dprep.items():
        if p.contains(pt): return k
    return 0

# ---------------- edge classes ----------------
tot_len = Counter()
for u, v, ni, ty, wd, L, g in net['edges']: tot_len[names[ni][0]] += L
def eclass(ni, wd):
    nm = names[ni][0]
    if wd >= 15 or (nm and tot_len[nm] > 8000): return 'A'
    return 'B' if nm else 'C'

# ---------------- keep edges inside BKK (+small buffer), drop Chao Phraya ----------------
G = nx.MultiGraph()
cp_nodes = set(); outside_edges = []
for i, (u, v, ni, ty, wd, L, g) in enumerate(net['edges']):
    nm = names[ni][0]
    if nm in CP_NAMES:
        cp_nodes.update([u, v]); continue
    mu, mv = nodes[u], nodes[v]
    inu, inv = bkk_buf.contains(Point(*mu)), bkk_buf.contains(Point(*mv))
    if not (inu or inv):
        continue
    if inu != inv:
        outside_edges.append((u, v, ni, inu))
    pts = [nodes[u]] + [(g[k] / 1e4, g[k + 1] / 1e4) for k in range(0, len(g), 2)] + [nodes[v]]
    G.add_edge(u, v, key=i, ni=ni, cls=eclass(ni, wd), L=max(L, 1), pts=pts)
print('model graph', G.number_of_nodes(), G.number_of_edges())

# ---------------- stations: snap to nearest node, prefer same canal name ----------------
stations = list(csv.DictReader(open(P('canal_station.csv'), encoding='utf-8-sig')))
gnodes = list(G.nodes()); garr = np.array([xy(*nodes[n]) for n in gnodes]); tree = cKDTree(garr)
node_names = defaultdict(set)
for u, v, k, d in G.edges(keys=True, data=True):
    nm = names[d['ni']][0]
    if nm: node_names[u].add(nm); node_names[v].add(nm)
TONE = re.compile('[่-์]')
def key(s): return TONE.sub('', (s or '').replace(' ', ''))

station_node = {}
for s in stations:
    if not s['lat'] or s['in_bkk'] != '1': continue
    p = xy(float(s['lon']), float(s['lat']))
    dd, ii = tree.query(p, k=12, distance_upper_bound=1500)
    best = None
    for d0, i0 in zip(dd, ii):
        if not np.isfinite(d0): break
        n = gnodes[i0]; same = any(key(s['river']) and key(s['river']) in key(x) for x in node_names[n])
        score = d0 - (600 if same else 0)
        if best is None or score < best[0]: best = (score, n, d0)
    if best and best[2] < 1200: station_node[s['code']] = best[1]
print('stations snapped', len(station_node))

# ---------------- pump sinks ----------------
pumps = []
cap_tel = {}
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')):
    c = float(r['capacity_m3'] or 0)
    if c > 0: cap_tel[r['code']] = max(cap_tel.get(r['code'], 0), c / 86400)
obs_max = defaultdict(float)
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')):
    obs_max[r['code']] = max(obs_max[r['code']], float(r['discharged_m3'] or 0) / 86400)
st_by = {s['code']: s for s in stations}
tel_keys = {}
for code, cap in cap_tel.items():
    s = st_by.get(code)
    if not s or code not in station_node: continue
    pumps.append(dict(id=code, name=s['name'], node=station_node[code], cap=round(cap, 2), src='telemetry',
                      obs_max=round(obs_max[code], 2), internal=code in INTERNAL_PUMPS))
    tel_keys[key(s['name'].replace('ส.', ''))] = code
# also telemetry stations with 0 capacity listed but have known report capacity are handled below by name

rep = json.load(open(P('pump_report2557.json')))
edges_by_name = defaultdict(list)
for u, v, k, d in G.edges(keys=True, data=True):
    edges_by_name[key(names[d['ni']][0])].append((u, v))
cp_tree = cKDTree(np.array([xy(*nodes[n]) for n in cp_nodes]))

def locate(canal, target):
    """node on `canal` touching `target` (None = Chao Phraya)."""
    cand = {n for e in edges_by_name.get(key(canal), []) for n in e}
    if not cand: return None
    if target is None:
        best = min(cand, key=lambda n: cp_tree.query(xy(*nodes[n]))[0]); dist = cp_tree.query(xy(*nodes[best]))[0]
    else:
        tn = {n for e in edges_by_name.get(key(target), []) for n in e}
        if not tn: return None
        common = cand & tn
        if common: return sorted(common)[0]
        tt = cKDTree(np.array([xy(*nodes[n]) for n in tn]))
        best = min(cand, key=lambda n: tt.query(xy(*nodes[n]))[0]); dist = tt.query(xy(*nodes[best]))[0]
    return best if dist < 400 else None

unplaced = []
for r in rep:
    nm = re.sub(r'^(สถานีสูบน้ำ|สถานีสุบน้ำ|สถานีสบู น้าํ|ปตร\.|อาคารสูบน้ำ)\s*', '', r['name'])
    m = re.match(r'(.*?)\s*\((.*)\)', nm); target = None
    if m:
        nm = m[1]; target = m[2] if m[2].startswith('คลอง') else None
    k0 = key(nm)
    if any(k0 and (k0 in tk or tk in k0) for tk in tel_keys):
        continue  # already in telemetry
    canal = nm.split(' ')[0]
    if not canal.startswith('คลอง') and not canal.startswith('คู'): canal = 'คลอง' + canal
    n = None
    for c in (canal, re.sub(r'(เกา|ใหม|เกา่|ใหม่)$', '', canal), re.sub(r'^คลอง', 'คลอง', canal.replace('วัด', ''))):
        n = locate(c, target)
        if n is not None: break
    if n is None: unplaced.append(r['name']); continue
    pumps.append(dict(id='R%s%02d' % (r['side'], r['no']), name=r['name'], node=n, cap=r['cap'], src='report2557',
                      obs_max=None, internal=False))
print('pumps', len(pumps), 'unplaced report stations', len(unplaced), unplaced)

# ---------------- boundary outlets (assumption) ----------------
outlets = []
seen = set()
for u, v, ni, inside_u in outside_edges:
    nm = names[ni][0]; n_in = u if inside_u else v; n_out = v if inside_u else u
    if nodes[n_out][1] > SEA_LAT or eclass(ni, 0) == 'C': continue
    if nm in seen or n_in not in G: continue
    seen.add(nm)
    outlets.append(dict(id='OUT%d' % len(outlets), name=f'ทางออกขอบเขต {nm}', node=n_in, canal=nm))
print('boundary outlets', len(outlets))

# ---------------- contract degree-2 nodes ----------------
special = set(station_node.values()) | {p['node'] for p in pumps} | {o['node'] for o in outlets}
H = nx.Graph(); EID = 0; E = []
def add(u, v, ni, cls, L, pts):
    E.append(dict(u=u, v=v, ni=ni, cls=cls, L=L, pts=pts))
visited = set()
deg = dict(G.degree())
def walk(u, k0, v, d0):
    pts = list(d0['pts']) if (G.nodes and True) else []
    # orient pts u->v
    if np.allclose(pts[0], nodes[u]) is False and np.allclose(pts[-1], nodes[u]): pts = pts[::-1]
    L = d0['L']; cls = d0['cls']; ni = d0['ni']; prev = u; cur = v
    visited.add(k0)
    while deg[cur] == 2 and cur not in special:
        nxt = [(a, b, k, d) for a, b, k, d in G.edges(cur, keys=True, data=True) if k not in visited]
        if not nxt: break
        a, b, k, d = nxt[0]
        if d['cls'] != cls or d['ni'] != ni: break
        visited.add(k); q = list(d['pts'])
        if not np.allclose(q[0], nodes[cur]): q = q[::-1]
        pts += q[1:]; L += d['L']; prev, cur = cur, (b if a == cur else a)
    return cur, pts, L, cls, ni
for u, v, k, d in list(G.edges(keys=True, data=True)):
    if k in visited: continue
    if deg[u] == 2 and u not in special and deg[v] == 2 and v not in special:
        continue  # interior of a chain, picked up from an end
    a = u if (deg[u] != 2 or u in special) else v; b = v if a == u else u
    end, pts, L, cls, ni = walk(a, k, b, d)
    if end != a or len(pts) > 2: add(a, end, ni, cls, L, pts)
# isolated loops of degree-2 nodes
for u, v, k, d in G.edges(keys=True, data=True):
    if k not in visited: visited.add(k); add(u, v, d['ni'], d['cls'], d['L'], d['pts'])
print('contracted edges', len(E))

# ---------------- renumber nodes ----------------
used = sorted({e['u'] for e in E} | {e['v'] for e in E} | special)
idx = {n: i for i, n in enumerate(used)}
N = [dict(lon=round(nodes[n][0], 5), lat=round(nodes[n][1], 5)) for n in used]
for e in E: e['u'] = idx[e['u']]; e['v'] = idx[e['v']]
for p in pumps: p['node'] = idx[p['node']]
for o in outlets: o['node'] = idx[o['node']]
station_node = {c: idx[n] for c, n in station_node.items()}

# components with a sink
Gc = nx.Graph(); Gc.add_nodes_from(range(len(N))); Gc.add_edges_from((e['u'], e['v']) for e in E)
sink_nodes = {p['node'] for p in pumps if not p['internal']} | {o['node'] for o in outlets}
good = set()
for comp in nx.connected_components(Gc):
    if comp & sink_nodes: good |= comp
print('nodes in drained components', len(good), 'of', len(N))

# ---------------- catchments: grid cells -> nearest node in a drained component ----------------
minx, miny, maxx, maxy = bkk.bounds
gx = GRID_M / KX; gy = GRID_M / KY
cells = [(x, y) for x in np.arange(minx + gx / 2, maxx, gx) for y in np.arange(miny + gy / 2, maxy, gy) if bkk_p.contains(Point(x, y))]
gl = sorted(good); gtree = cKDTree(np.array([xy(N[i]['lon'], N[i]['lat']) for i in gl]))
_, ci = gtree.query(np.array([xy(x, y) for x, y in cells]))
area = Counter(); cell_d = defaultdict(Counter)
for (x, y), k in zip(cells, ci):
    area[gl[k]] += GRID_M * GRID_M
cell_area_km2 = GRID_M * GRID_M / 1e6
print('catchment km2', round(sum(area.values()) / 1e6, 1), 'cells', len(cells))
for i, n in enumerate(N):
    n['A'] = round(area.get(i, 0) / 1e6, 4)    # km2
    n['d'] = district_of(n['lon'], n['lat'])

# ---------------- canal surface (storage) per node: half of incident edges ----------------
for e in E:
    c = CLASS[e['cls']]; s = e['L'] * c['w'] / 2
    N[e['u']]['S'] = N[e['u']].get('S', 0) + s; N[e['v']]['S'] = N[e['v']].get('S', 0) + s
for n in N: n['S'] = round(n.get('S', 0))

# ---------------- nearest rain gauges (3, inverse distance) ----------------
rain = list(csv.DictReader(open(P('rain_station.csv'), encoding='utf-8-sig')))
rarr = np.array([xy(float(r['lon']), float(r['lat'])) for r in rain]); rtree = cKDTree(rarr)
for n in N:
    d, i = rtree.query(xy(n['lon'], n['lat']), k=3)
    w = 1 / np.maximum(d, 300) ** 2; w = w / w.sum()
    n['rg'] = [[rain[j]['code'], round(float(ww), 3)] for j, ww in zip(i, w)]

# ---------------- level station per node (nearest snapped station, same component, < 2.5 km) ----------------
snodes = [(c, n) for c, n in station_node.items()]
sarr = np.array([xy(N[n]['lon'], N[n]['lat']) for c, n in snodes]); stree = cKDTree(sarr)
for i, n in enumerate(N):
    d, j = stree.query(xy(n['lon'], n['lat']))
    if d < 2500: n['st'] = snodes[j][0]

# ---------------- feeder canals: a canal that feeds a pump is assumed able to deliver the pump's capacity ----------------
# capacities are expressed at the reference velocity V0 (0.5 m/s); the page rescales by v/V0
V0 = 0.5
for e in E:
    c = CLASS[e['cls']]; e['cap0'] = V0 * c['w'] * c['d']
adj = defaultdict(list)
for k, e in enumerate(E): adj[e['u']].append(k); adj[e['v']].append(k)
FEED_M = 6000
for p in pumps:
    if p['internal']: continue
    cap = p['cap']; start = p['node']
    # walk outward along edges, following the widest class first, up to FEED_M metres
    seen_n = {start: 0}; frontier = [start]
    while frontier:
        n = frontier.pop()
        for k in adj[n]:
            e = E[k]; m = e['v'] if e['u'] == n else e['u']; dist = seen_n[n] + e['L']
            if e['cls'] == 'C' or dist > FEED_M: continue
            share = cap if seen_n[n] == 0 else cap * max(0.3, 1 - dist / FEED_M)
            e['cap0'] = max(e['cap0'], share)
            if m not in seen_n or seen_n[m] > dist: seen_n[m] = dist; frontier.append(m)

# ---------------- edge -> level station on the same canal (for map colouring) ----------------
st_by_canal = defaultdict(list)
for c, n in station_node.items():
    s = st_by[c]; st_by_canal[key(s['river'])].append((c, xy(N[n]['lon'], N[n]['lat'])))
for e in E:
    nm = key(names[e['ni']][0]); cand = st_by_canal.get(nm, [])
    if not nm or not cand: continue
    mid = e['pts'][len(e['pts']) // 2]; mx, my = xy(*mid)
    c, (sx, sy) = min(cand, key=lambda t: (t[1][0] - mx) ** 2 + (t[1][1] - my) ** 2)
    if math.hypot(sx - mx, sy - my) < 3000: e['st'] = c

# ---------------- Chao Phraya polylines for the map ----------------
CPL = []
for (u, v, ni, ty, wd, L, gg) in net['edges']:
    if names[ni][0] not in CP_NAMES: continue
    pts = [nodes[u]] + [(gg[k] / 1e4, gg[k + 1] / 1e4) for k in range(0, len(gg), 2)] + [nodes[v]]
    CPL.append([[round(x, 4), round(y, 4)] for x, y in pts])

# ---------------- output ----------------
out = dict(
    source='OpenStreetMap (Overpass API, ODbL) ดึง 3 ต.ค. 2569 · สำนักการระบายน้ำ กทม. · แผนปฏิบัติการป้องกันและแก้ไขปัญหาน้ำท่วม กทม. 2557',
    classes=CLASS, names=[n[0] for n in names],
    nodes=[dict(i=i, **n) for i, n in enumerate(N)],
    edges=[dict(u=e['u'], v=e['v'], n=e['ni'], c=e['cls'], L=round(e['L']), cap0=round(e['cap0'], 2), st=e.get('st'),
                g=[[round(x, 4), round(y, 4)] for x, y in e['pts']]) for e in E],
    chao_phraya=CPL,
    V0=V0,
    pumps=pumps, outlets=outlets, station_node=station_node,
    drained=sorted(good), grid_m=GRID_M,
)
json.dump(out, open(P('network.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
print('network.json KB', os.path.getsize(P('network.json')) // 1024)
