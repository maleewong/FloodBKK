"""Flood ponding hotspots for the field page (part 2): where water is trapped now, how much, and where it can go.

Inputs (data/): traffy_flood.json   flood reports from Traffy Fondue (fetched in the browser; see NOTES)
                osm_residential.json named housing estates (OSM landuse=residential polygons)
                dem.json            ground-surface estimate (Copernicus GLO-30, see build_dem.py)
                network.json, model_data.json (canal levels), plan.json (canal flows of the 30-day plan)
Output: data/ponding.json
Needs numpy, scipy, shapely, scikit-learn (one-off / when new reports are fetched)
"""
import json, os, re, base64, math, collections
import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Polygon, MultiPoint, Point
from shapely.ops import unary_union
from sklearn.cluster import DBSCAN

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574
xy = lambda lon, lat: (lon * KX, lat * KY)

T = json.load(open(P('traffy_flood.json'), encoding='utf-8'))
rows = T['rows']                                             # ticket, lon, lat, 'MM-DD hh:mm', state, district, desc
from datetime import datetime, timedelta
RECENT = (datetime.strptime(T['fetched'][:10], '%Y-%m-%d') - timedelta(days=4)).strftime('%m-%d')   # last ~5 days, not closed
act = [r for r in rows if r[4] < 4 and r[3] >= RECENT and 100.3 < r[1] < 101 and 13.4 < r[2] < 14.1]
X = np.array([xy(r[1], r[2]) for r in act])
lab = DBSCAN(eps=250, min_samples=5).fit(X).labels_
print('active recent reports', len(act), 'clusters', lab.max() + 1)

# ---------- DEM ----------
D = json.load(open(P('dem.json')))
dem = np.frombuffer(base64.b64decode(D['b64']), dtype='<i2').reshape(D['h'], D['w']) / 100.0
GR = np.load(P('dem_ground.npy')).astype(float) / 100.0 if os.path.exists(P('dem_ground.npy')) else dem
_wb = np.frombuffer(base64.b64decode(D['water']), dtype=np.uint8) if 'water' in D else None
WATER = (np.unpackbits(_wb)[:D['w'] * D['h']].reshape(D['h'], D['w']) == 1) if _wb is not None else np.zeros_like(GR, bool)
CELL_M2 = (D['cell'] * KX) * (D['cell'] * KY)
def cell(lon, lat):
    return int(np.clip((D['lat0'] - lat) / D['cell'], 0, D['h'] - 1)), int(np.clip((lon - D['lon0']) / D['cell'], 0, D['w'] - 1))
FLOOD = np.zeros_like(GR)          # estimated depth (m) of the flooded cells, all hotspots
def flood_fill(pts, depths, lon, lat, rmax, zone=None):
    """water surface = median(ground at report + reported depth); grow from the report cells over lower ground"""
    cells = [cell(r[1], r[2]) for r in pts]
    g = np.array([GR[c] for c in cells]); wse_each = g + np.array(depths)
    wse = float(np.median(wse_each)); dmed = float(np.median(depths)); wse = min(wse, float(np.median(g)) + 1.5)
    j0, i0 = cell(lon, lat); R = int(rmax / (D['cell'] * KY)) + 1
    seen = set(); stack = [c for c in set(cells) if GR[c] < wse and not WATER[c]]; got = []
    while stack:
        c = stack.pop()
        if c in seen: continue
        seen.add(c)
        if abs(c[0] - j0) > R or abs(c[1] - i0) > R or WATER[c] or GR[c] >= wse: continue
        if zone is not None:
            clon, clat = D['lon0'] + (c[1] + 0.5) * D['cell'], D['lat0'] - (c[0] + 0.5) * D['cell']
            if not zone.contains(Point(clon * KX, clat * KY)): continue
        got.append(c)
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            q = (c[0] + dj, c[1] + di)
            if 0 <= q[0] < D['h'] and 0 <= q[1] < D['w'] and q not in seen: stack.append(q)
    dep = np.array([min(2 * dmed, 1.5, wse - GR[c]) for c in got]) if got else np.zeros(0)   # no deeper than twice the reported depth
    keep = dep >= 0.05
    got = [c for c, k in zip(got, keep) if k]; dep = dep[keep]
    for c, d in zip(got, dep): FLOOD[c] = max(FLOOD[c], d)
    return wse, got, dep

# road flood sensors (BMA) as an independent check of the reports
SNAP = json.load(open(P('snapshot.json'), encoding='utf-8'))
ROAD = [(f[2], f[7], f[6], f[9], f[12], f[8]) for f in SNAP.get('F', []) if f[6] and f[7]]
FETCH = datetime.strptime(T['fetched'][:16], '%Y-%m-%d %H:%M')
def age_h(mmddhhmm):
    try: t = datetime.strptime(f'{FETCH.year}-{mmddhhmm}', '%Y-%m-%d %H:%M')
    except ValueError: return None
    return (FETCH - t).total_seconds() / 3600

def ground(lon, lat):
    j = (D['lat0'] - lat) / D['cell']; i = (lon - D['lon0']) / D['cell']
    i0, j0 = int(np.clip(i, 0, D['w'] - 1)), int(np.clip(j, 0, D['h'] - 1))
    return float(dem[j0, i0])

# ---------- housing estates ----------
RES = json.load(open(P('osm_residential.json'), encoding='utf-8'))
polys = []
for name, en, g in RES:
    if len(g) < 4: continue
    try:
        pg = Polygon([xy(*p) for p in g])
        if pg.is_valid and pg.area > 5000: polys.append((name, en, pg, g))
    except Exception:
        pass
ptree = cKDTree(np.array([[p[2].centroid.x, p[2].centroid.y] for p in polys]))

# ---------- depth / duration from the report text ----------
WORD = [('หน้าอก', 1.2), ('ระดับอก', 1.2), ('ถึงอก', 1.2), ('เอว', 0.9), ('ต้นขา', 0.7), ('เข่า', 0.5), ('หน้าแข้ง', 0.3), ('ข้อเท้า', 0.15), ('ตาตุ่ม', 0.1)]
def depth_of(t):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:-\s*\d+\s*)?(ซม|เซน|cm|ซ\.ม)', t)
    if m: v = float(m[1]) / 100; return v if 0.05 <= v <= 2 else None
    m = re.search(r'(\d+(?:\.\d+)?)\s*เมตร', t)
    if m: v = float(m[1]); return v if 0.05 <= v <= 2 else None
    for w, v in WORD:
        if w in t: return v
    return None
def days_of(t):
    m = re.search(r'(\d+)\s*วัน', t); return int(m[1]) if m and int(m[1]) < 30 else None

# ---------- canal network ----------
net = json.load(open(P('network.json'), encoding='utf-8'))
M = json.load(open(P('model_data.json'), encoding='utf-8'))
N = net['nodes']; E = net['edges']; names = net['names']
node_canal = collections.defaultdict(set)
for k, e in enumerate(E):
    if e['c'] in ('A', 'B'): node_canal[e['u']].add((names[e['n']], e['c'])); node_canal[e['v']].add((names[e['n']], e['c']))
cand_nodes = [i for i in node_canal]
ctree = cKDTree(np.array([xy(N[i]['lon'], N[i]['lat']) for i in cand_nodes]))
geo = {int(g[0][2:]): g[1] for g in M['geo']}

hot = []
for c in range(lab.max() + 1):
    idx = np.where(lab == c)[0]; pts = [act[i] for i in idx]
    cx, cy = np.median(X[idx, 0]), np.median(X[idx, 1]); lon, lat = cx / KX, cy / KY
    mp = MultiPoint([tuple(X[i]) for i in idx])
    # estates that contain >= 3 reports or the centroid
    near = ptree.query_ball_point((cx, cy), 1500)
    est = []
    for j in near:
        nm, en, pg, g = polys[j]
        k = sum(pg.buffer(60).contains(Point(*X[i])) for i in idx)
        if k >= 3: est.append((k, nm, en, pg, g))
    est.sort(key=lambda t: -t[0])
    hull = mp.convex_hull.buffer(100)
    area_geom = unary_union([t[3] for t in est[:6]] + [hull]) if est else hull
    area = min(area_geom.area, 3e6)
    texts = [r[6] for r in pts if r[6]]
    deps = [d for d in (depth_of(t) for t in texts) if d]
    # days flooded = max(time since the first Traffy report inside this area (any state, whole fetch window),
    #                    "flooded for N days" in a report + how old that report is)
    from shapely.prepared import prep as _prep
    zone_all = area_geom.buffer(150); zp = _prep(zone_all); bx = zone_all.bounds
    t_all = [r[3] for r in rows if r[3] and bx[0] <= r[1] * KX <= bx[2] and bx[1] <= r[2] * KY <= bx[3] and zp.contains(Point(*xy(r[1], r[2])))]
    # start of the current episode: walk back from the newest report until a gap of more than 48 h
    first_time = None
    ts = sorted(t_all)
    if ts:
        first_time = ts[-1]
        for prev in reversed(ts[:-1]):
            a0, a1 = age_h(prev), age_h(first_time)
            if a0 is None or a1 is None or a0 - a1 > 48: break
            first_time = prev
    fa = age_h(first_time) if first_time else None
    dys = [d + (age_h(r[3]) or 0) / 24 for r in pts for d in [days_of(r[6]) if r[6] else None] if d]
    days_est = max([x for x in (fa / 24 if fa is not None else None, max(dys) if dys else None) if x is not None], default=None)
    depth = float(np.median(deps)) if deps else 0.4
    dist = collections.Counter(r[5] for r in pts).most_common(1)[0][0]
    # name: what residents call it in the reports (most frequent "หมู่บ้าน/เคหะ/แฟลต/ชุมชน ..." token), else the OSM estate
    tok = collections.Counter()
    for t in texts:
        for m in re.finditer(r'(?:หมู่บ้าน|มบ\.|ม\.บ\.|เคหะ|แฟลต|ชุมชน)\s*([฀-๿a-zA-Z]{3,}(?:\s?\d+)?)', t):
            w = m.group(0).replace(' ', '')
            w = re.sub(r'^(มบ\.|ม\.บ\.)', 'หมู่บ้าน', w)
            rem = re.sub(r'^(หมู่บ้าน|เคหะ|แฟลต|ชุมชน)', '', w)
            if len(rem) < 3 or re.match(r'(อยู่|ใน|เรา|ของ|นี้|ที่|ผม|ฉัน|ตอน|ตรง|ทั้ง|ก็|จะ|มี|ได้|เป็น|ความ|ระดับ|น้ำ)', rem): continue
            tok[w[:26]] += 1
    names_txt = []
    for w, k in tok.most_common(4):
        w = re.sub(r'(น้ำ|ท่วม|ซอย|ความ).*$', '', w)
        if k >= 2 and len(w) > 5 and not any(w[:9] == v[:9] for v in names_txt): names_txt.append(w)
    name = ' / '.join(names_txt[:2]) if names_txt else (est[0][1] if est else None)
    if not name:
        j = ptree.query((cx, cy))[1]; nm0, _, pg0, _ = polys[j]
        name = f'ใกล้{nm0}' if pg0.distance(Point(cx, cy)) < 400 else f'ชุมชนในเขต{dist}'
    g0 = ground(lon, lat)
    # outlet candidates: nearest node of each distinct named canal within 2 km
    dd, ii = ctree.query((cx, cy), k=40, distance_upper_bound=2000)
    cands, seen = [], set()
    for d0, i0 in zip(dd, ii):
        if not np.isfinite(d0): break
        n = cand_nodes[i0]
        for cn, cl in sorted(node_canal[n], key=lambda t: t[1]):
            key = cn or f'คลองไม่มีชื่อ{n}'
            if key in seen: continue
            seen.add(key)
            st = N[n].get('st'); s = M['st'].get(st) if st else None
            lvl = s[1] if s and s[4] != 'ขัดข้อง' else None
            cands.append(dict(node=n, canal=cn or 'คลองไม่มีชื่อ', cls=cl, dist=round(float(d0)), level=lvl,
                              warn=s[2] if s else None, station=s[0] if s else None))
        if len(cands) >= 4: break
    # depth of each report (text, else the cluster median) -> DEM flood extent
    deps_each = [depth_of(r[6]) or depth for r in pts]
    rmax = min(1500, max(600, 1.5 * math.sqrt(area / math.pi)))
    from shapely.prepared import prep
    wse, fcells, fdep = flood_fill(pts, deps_each, lon, lat, rmax, prep(area_geom.buffer(250)))
    # freshness: last report, reports in the last 24/48 h, road sensors nearby
    ages = [a for a in (age_h(r[3]) for r in pts) if a is not None]
    last_age = min(ages) if ages else None
    n24 = sum(a <= 24 for a in ages); n48 = sum(a <= 48 for a in ages)
    sens = []
    for nm, slon, slat, cm, stt, ts in ROAD:
        dd = math.hypot((slon - lon) * KX, (slat - lat) * KY)
        if dd < 1500: sens.append(dict(name=nm, dist=round(dd), cm=cm, status=stt, time=ts))
    sens.sort(key=lambda x: x['dist'])
    s_flood = any(x['status'] in ('Flood', 'Minor flooding') or (x['cm'] or 0) >= 5 for x in sens if 'out of order' not in x['status'].lower())
    s_dry = [x for x in sens if x['status'] == 'Normal' and (x['cm'] or 0) < 5]
    if n24 >= 2 or s_flood: fresh = 'ยืนยัน'            # still flooded: new reports in 24 h or a road sensor shows water
    elif n48 >= 1: fresh = 'น่าจะยังท่วม'
    else: fresh = 'อาจลดแล้ว'
    if fresh != 'ยืนยัน' and s_dry and s_dry[0]['dist'] < 800: fresh = 'อาจลดแล้ว'
    shp = area_geom.simplify(40)
    ring = list(shp.exterior.coords) if shp.geom_type == 'Polygon' else list(max(shp.geoms, key=lambda q: q.area).exterior.coords)
    hot.append(dict(id=f'H{c + 1}', name=name, estates=[t[1] for t in est[:4]], district=dist,
                    lon=round(lon, 5), lat=round(lat, 5), n=len(pts), depth=round(depth, 2), depth_n=len(deps),
                    days=int(days_est) if days_est is not None else None, first_time=(f"{FETCH.year}-{first_time}" if first_time else None), area_km2=round(area / 1e6, 3), ground=round(g0, 2),
                    volume=round(area * depth * 0.7), cands=cands,
                    ring=[[round(x / KX, 5), round(y / KY, 5)] for x, y in ring],
                    quotes=[t for t in texts if depth_of(t) or 'วัน' in t][:3],
                    wse=round(wse, 2), dem_cells=len(fcells), dem_area_km2=round(len(fcells) * CELL_M2 / 1e6, 3),
                    dem_depth=round(float(fdep.mean()), 2) if len(fdep) else None,
                    vol_dem=round(float(fdep.sum() * CELL_M2)) if len(fdep) else None,
                    last_age_h=round(last_age, 1) if last_age is not None else None, last_time=(f"{FETCH.year}-{max(r[3] for r in pts)}" if pts else None), n24=n24, n48=n48, fresh=fresh, sensors=sens[:3]))
hot.sort(key=lambda h: -h['volume'])
pts_out = [[r[1], r[2], r[3][:5]] for r in act]
fl_idx = np.flatnonzero(FLOOD.ravel() >= 0.05)
flood_out = dict(w=D['w'], h=D['h'], lon0=D['lon0'], lat0=D['lat0'], cell=D['cell'],
                 idx=[int(i) for i in fl_idx], cm=[int(round(FLOOD.ravel()[i] * 100)) for i in fl_idx])
out = dict(flood=flood_out, source=T['source'], fetched=T['fetched'], recent_from=RECENT, n_reports=len(act), hotspots=hot, points=pts_out,
           fill=0.7, dem_source=D['source'])
json.dump(out, open(P('ponding.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
print('hotspots', len(hot), 'total volume Mm3 (area x depth x 0.7)', round(sum(h['volume'] for h in hot) / 1e6, 2),
      'DEM', round(sum(h['vol_dem'] or 0 for h in hot) / 1e6, 2), 'flooded cells', len(fl_idx), collections.Counter(h['fresh'] for h in hot))
for h in hot[:20]:
    print(h['id'], h['name'][:30], h['district'], h['n'], 'depth', h['depth'], 'area', h['area_km2'], 'vol', h['volume'], 'dem', h['dem_area_km2'], h['dem_depth'], h['vol_dem'], h['fresh'], h['last_age_h'], [(c['canal'], c['dist'], c['level']) for c in h['cands'][:3]])
