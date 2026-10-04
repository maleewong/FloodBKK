"""Flood map for the forecast page: where water is expected to stand in the next 48 h, how deep, and which roads.

Works only from measured data and the rain forecast (no citizen reports needed), district by district:
  1. rain the drains cannot take = 24-h forecast rain - local drain limit, summed into a volume per district
     local drain limit = the 24-h rain at which nearby road-flood sensors started to flood in the past
     (recomputed from the sensor history on every update, so it keeps itself up to date)
  2. canal water above the critical level per district (forecast_lp.py: measured levels + pumping the model advises)
  3. the volume of the district fills its lowest ground first (DEM, Copernicus GLO-30): from the canals over connected
     low land within 2 km when canal water dominates, else at the low ground of the district
     stations above critical in districts without model volume: overflow along that canal (<= 400 m)
  4. roads flooded now (road sensors) stay flooded at least at the measured depth
Traffy Fondue reports are NOT used to make the map; they are only used afterwards to check it (hit rate).

Inputs : data/forecast.json, forecast_raw.json, model_data.json, dem.json, dem_ground.npy, road_sensor.csv,
         road_flood_event.csv, rain_daily_top50.csv, rain_station.csv, road_net.json (optional), traffy_flood.json (optional)
Output : data/flood.json
Needs numpy.
"""
import base64, collections, csv, json, math, os
from datetime import datetime, timedelta
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574
CAN_R = 1000              # m, reach of canal overflow from a station
OVR = 0.5                 # m, cap of the overflow height used (the canal model is not reliable above this)
DMAX = 1.5                # m, cap of any estimated depth
FLOOR = 20.0              # mm/24 h, lowest drain limit used
CONC = 2.0                # the excess gathers on the lower half of the ground (roads, sois, ground floors): depth = 2 x even layer
SUAN_SIAM = (100.6926, 13.8067)

D = json.load(open(P('dem.json')))
H, W, CELL, LON0, LAT0 = D['h'], D['w'], D['cell'], D['lon0'], D['lat0']
GR = np.load(P('dem_ground.npy')).astype(float) / 100.0
_wb = np.frombuffer(base64.b64decode(D['water']), dtype=np.uint8) if 'water' in D else None
WATER = (np.unpackbits(_wb)[:W * H].reshape(H, W) == 1) if _wb is not None else np.zeros((H, W), bool)
CLON = LON0 + (np.arange(W) + 0.5) * CELL; CLAT = LAT0 - (np.arange(H) + 0.5) * CELL
LON, LAT = np.meshgrid(CLON, CLAT)
CELL_M = CELL * KY


def cell(lon, lat):
    return int(np.clip((LAT0 - lat) / CELL, 0, H - 1)), int(np.clip((lon - LON0) / CELL, 0, W - 1))


def idw(px, py, val, k=6, pw=2.0):
    """inverse-distance interpolation of point values onto the DEM grid (k nearest points)"""
    px, py, val = np.asarray(px, float), np.asarray(py, float), np.asarray(val, float)
    d2 = ((LON[..., None] - px) * KX) ** 2 + ((LAT[..., None] - py) * KY) ** 2
    k = min(k, len(px)); idx = np.argpartition(d2, k - 1, axis=-1)[..., :k]
    dk = np.take_along_axis(d2, idx, -1); w = 1 / np.maximum(dk, 100.0) ** (pw / 2)
    return (w * val[idx]).sum(-1) / w.sum(-1)


# ---------- drain limit from the road-sensor history ----------
def drain_limit(before=None):
    rain = collections.defaultdict(dict); days = set()
    for r in csv.DictReader(open(P('rain_daily_top50.csv'), encoding='utf-8-sig')):
        if not r['rf24h_mm'] or (before and r['day'] >= before): continue
        days.add(r['day']); rain[r['day']][r['district']] = max(rain[r['day']].get(r['district'], 0.0), float(r['rf24h_mm']))
    prev = {d: (datetime.fromisoformat(d) - timedelta(days=1)).strftime('%Y-%m-%d') for d in days}
    rd = lambda d, k: max(rain.get(d, {}).get(k, 0.0), rain.get(prev.get(d, ''), {}).get(k, 0.0))
    sens = {r['code']: r for r in csv.DictReader(open(P('road_sensor.csv'), encoding='utf-8-sig'))}
    ev = collections.defaultdict(set)
    for r in csv.DictReader(open(P('road_flood_event.csv'), encoding='utf-8-sig')):
        d = r['start'][:10]
        if d in prev: ev[r['code']].add(d)
    pts = []
    for c, ds in ev.items():
        s = sens.get(c)
        if not s or len(ds) < 2: continue
        v = sorted(x for x in (rd(d, s['district']) for d in ds) if x >= 5)
        if len(v) < 2: continue
        pts.append((float(s['lon']), float(s['lat']), v[max(0, int(0.25 * (len(v) - 1)))], c))
    if len(pts) < 5: return np.full((H, W), 60.0), pts, sorted(days)
    lim = idw([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts], k=len(pts))
    return np.maximum(lim, FLOOR), pts, sorted(days)


# ---------- 1. rain the drains cannot take, left standing on the ground ----------
def excess_depth(r24, lim):
    """water left on the ground (m) = 24-h rain - local drain limit, spread evenly (no local redistribution: tested against
    the 25 Sep 2026 reports, local low spots in this 120-m DEM did not tell where reports came from, see NOTES)"""
    return np.minimum(CONC * np.maximum(0, r24 - lim) / 1000.0, DMAX)


def flood_fill(j0, i0, wse, rmax_m, depth_cap):
    """cells connected to (j0,i0) with ground below wse, within rmax_m"""
    R = rmax_m / CELL_M; seen = set(); st = [(j0, i0)]; out = []
    while st:
        c = st.pop()
        if c in seen: continue
        seen.add(c); j, i = c
        if (j - j0) ** 2 + (i - i0) ** 2 > R * R or GR[c] >= wse or WATER[c]: continue
        out.append(c)
        for q in ((j + 1, i), (j - 1, i), (j, i + 1), (j, i - 1)):
            if 0 <= q[0] < H and 0 <= q[1] < W and q not in seen: st.append(q)
    return [(c, min(depth_cap, wse - GR[c])) for c in out]


# ---------- 2. canal overflow ----------
MD = json.load(open(P('model_data.json'), encoding='utf-8'))
CELL_AREA = CELL_M * CELL * KX
DT_H = 3


def poly_mask(coords):
    """cells inside a (multi)polygon, scanline fill"""
    rings = []
    def walk(c):
        if isinstance(c[0][0], (int, float)): rings.append(np.array(c, float))
        else:
            for x in c: walk(x)
    walk(coords); mask = np.zeros((H, W), bool)
    ed = np.concatenate([np.stack([r, np.roll(r, -1, 0)], 1) for r in rings])
    x1, y1, x2, y2 = ed[:, 0, 0], ed[:, 0, 1], ed[:, 1, 0], ed[:, 1, 1]
    for j, la in enumerate(CLAT):
        k = (y1 <= la) != (y2 <= la)
        if not k.any(): continue
        xs = np.sort(x1[k] + (la - y1[k]) * (x2[k] - x1[k]) / (y2[k] - y1[k]))
        for a, b in zip(xs[0::2], xs[1::2]): mask[j, (CLON >= a) & (CLON <= b)] = True
    return mask


def raster_lines(lines):
    m = np.zeros((H, W), bool)
    for pts in lines:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot((x1 - x0) * KX, (y1 - y0) * KY) / 40))
            for t in np.linspace(0, 1, n + 1): m[cell(x0 + t * (x1 - x0), y0 + t * (y1 - y0))] = True
    return m


def grow(m, cells):
    """dilate a mask by about `cells` grid cells (4-neighbour steps)"""
    out = m.copy()
    for _ in range(int(cells)):
        o = out.copy(); o[1:] |= out[:-1]; o[:-1] |= out[1:]; o[:, 1:] |= out[:, :-1]; o[:, :-1] |= out[:, 1:]; out = o
    return out


def label(mask):
    """4-connected components (scipy if present, else a simple flood fill)"""
    try:
        from scipy.ndimage import label as _l
        return _l(mask)[0]
    except ImportError:
        lab = np.zeros(mask.shape, int); n = 0
        for j0, i0 in zip(*np.where(mask)):
            if lab[j0, i0]: continue
            n += 1; st = [(j0, i0)]
            while st:
                j, i = st.pop()
                if not (0 <= j < H and 0 <= i < W) or not mask[j, i] or lab[j, i]: continue
                lab[j, i] = n; st += [(j + 1, i), (j - 1, i), (j, i + 1), (j, i - 1)]
        return lab


DIST = {g[1]: poly_mask(g[2]) & ~WATER for g in MD['geo']}
CANAL = raster_lines([e[6] for e in MD['net']['edges'] if e[3] != 'C'])          # main and secondary canals
CANAL_ST = collections.defaultdict(list)
for e in MD['net']['edges']:
    if len(e) > 7 and e[7]: CANAL_ST[e[7]].append(e[6])
NEAR = grow(CANAL, 2000 / CELL_M)                                                   # land within ~2 km of a canal


def spread_volume(vol_m3, region, seed, cap=1.0, lmax=None):
    """water surface L so that the cells of `region` below L that connect to `seed` (the canals) hold vol_m3;
    returns depth grid (m). This is what makes the flood spread along the canals over low ground."""
    if vol_m3 <= 0 or not region.any(): return np.zeros((H, W))
    g = GR[region]; lo, hi = float(g.min()), float(g.min()) + 4.0
    if lmax is not None: hi = max(lo + 0.01, min(hi, lmax))
    def wet(L):
        m = region & (GR < L); lab = label(m); ids = np.unique(lab[m & seed]); ids = ids[ids > 0]
        return np.isin(lab, ids)
    for _ in range(22):
        L = (lo + hi) / 2; w = wet(L)
        v = float(np.minimum(cap, L - GR[w]).sum()) * CELL_AREA
        if v < vol_m3: lo = L
        else: hi = L
    w = wet(lo)
    out = np.zeros((H, W)); out[w] = np.minimum(cap, lo - GR[w]); return out


def canal_overflow(stations, districts, use_now=False, rain_ex=None, rain_on=None):
    """(a) districts where the canal model holds water above the critical level: spread that volume over low ground
           connected to the canals of the district (inside the district, within 2 km of a canal)
       (b) other stations above critical: water above the bank floods low ground within 400 m along that station's canal"""
    dep = np.zeros((H, W)); on = np.full((H, W), 255.0); vols = {}
    src = np.zeros((H, W)); byd = {d['district']: d for d in districts}
    for name, dm in DIST.items():
        d = byd.get(name, {}); s = d.get('crit_s_opt') or [d.get('crit_opt', 0) or 0]
        k = 0 if use_now else int(np.argmax(s)); vc = float(s[k]) * 1e6                 # canal water above critical (model)
        vr = float(rain_ex[dm].sum()) * CELL_AREA if rain_ex is not None else 0.0         # rain the drains cannot take
        if vc + vr < 1e4: continue
        lmax = None
        if vc >= vr:                                                                      # spreads from the canals
            region, seed = dm & NEAR, CANAL & dm
            # water from a canal cannot stand higher than the canal water: bank (ground at the canals) + the largest
            # measured/forecast excess over critical of the district's stations (<= OVR); the rest stays in the canals
            hs = [min(OVR, (r['now'] if use_now else max(r['now'], r['peak_opt'])) - r['crit']) for r in stations
                  if r.get('district') == name and r.get('crit') is not None]
            hmax = max([h for h in hs if h > 0], default=0.2)
            lmax = float(np.median(GR[seed])) + hmax if seed.any() else None
        else: region, seed = dm, dm                                                       # gathers at the low ground of the district
        dd = spread_volume(vc + vr, region, seed, lmax=lmax); dep = np.maximum(dep, dd)
        w = dd >= 0.05; src[w] = 2 if vc >= vr else 1
        kc = 0 if use_now else next((i for i, x in enumerate(s) if x * 1e6 >= 1e4), 0)
        kr = float(rain_on[dm].min()) if rain_on is not None and vr > 0 else 255
        on[w] = np.minimum(on[w], min(kc * DT_H if vc >= 1e4 else 255, kr))
        vols[name] = [round(vc / 1e6, 2), round(vr / 1e6, 2)]
    used = []
    for r in stations:
        if r.get('crit') is None or (r['district'] in vols and vols[r['district']][0] > 0): continue
        pk = r['now'] if use_now else max(r['now'], r['peak_opt']); h = min(OVR, pk - r['crit'])
        s = MD['st'].get(r['code'])
        if h < 0.05 or not s or s[8] is None: continue
        lines = CANAL_ST.get(r['code']) or []
        seed = raster_lines(lines) if lines else np.zeros((H, W), bool)
        j, i = cell(s[9], s[8]); seed[j, i] = True
        g0 = float(np.median(GR[seed]))
        region = grow(seed, 400 / CELL_M) & ~WATER & (GR < g0 + h)
        lab = label(region); ids = np.unique(lab[seed & region]); w = np.isin(lab, ids[ids > 0])
        dd = np.where(w, np.minimum(h + 0.3, g0 + h - GR), 0)
        dep = np.maximum(dep, dd)
        k0 = 0 if use_now else next((i for i, x in enumerate(r['series']) if x >= r['crit'] + 0.05), 0)
        w = dd >= 0.05; src[w & (src == 0)] = 2
        on[w] = np.minimum(on[w], k0 * DT_H); used.append(r['code'])
    return dep, on, used, vols, src


def road_now(model_road):
    dep = np.zeros((H, W))
    for name, la, lo, cm, stt in model_road:
        if not cm or cm <= 0: continue
        j, i = cell(lo, la)
        for c, d in flood_fill(j, i, GR[j, i] + cm / 100, 500, cm / 100):
            dep[c] = max(dep[c], d)
    return dep


# ---------- roads on the flood map ----------
RN = json.load(open(P('road_net.json'), encoding='utf-8')) if os.path.exists(P('road_net.json')) else None
def flooded_roads(dep, min_m=0.10, cap=2000):
    """road pieces standing in >= min_m of water: [name, class, max depth cm, length m, [[lon, lat], ...]] (deepest first, at most cap)
    and the same summed per road name (for the list)"""
    if not RN: return [], []
    out = []
    def close(run, best):
        if len(run) > 1:
            L = sum(math.hypot((b[0] - a[0]) * KX, (b[1] - a[1]) * KY) for a, b in zip(run, run[1:]))
            out.append([nm, cls, round(best * 100), round(L), run])
    for nm, cls, pts in RN['ways']:
        run = []; best = 0.0
        for x, y in pts:
            d = float(dep[cell(x, y)])
            if d >= min_m: run.append([x, y]); best = max(best, d)
            else:
                close(run, best); run = []; best = 0.0
        close(run, best)
    out.sort(key=lambda r: (-r[2], -r[3]))
    by = collections.OrderedDict()
    for nm, cls, cm, L, run in out:
        if not nm: continue
        b = by.setdefault(nm, [nm, cls, 0, 0.0, run[len(run) // 2]]); b[2] = max(b[2], cm); b[3] += L
    names = sorted(by.values(), key=lambda r: (-r[2], -r[3]))[:40]
    return out[:cap], [[n, c, cm, round(L / 1000, 1), at] for n, c, cm, L, at in names]


def city_mask():
    """cells inside the BMA district polygons (scanline fill, numpy only)"""
    M = json.load(open(P('model_data.json'), encoding='utf-8')); mask = np.zeros((H, W), bool)
    rings = []
    def walk(c):
        if isinstance(c[0][0], (int, float)): rings.append(np.array(c, float))
        else:
            for x in c: walk(x)
    for g in M['geo']: walk(g[2])
    edges = np.concatenate([np.stack([r[:-1], r[1:]], 1) if np.allclose(r[0], r[-1]) else np.stack([r, np.roll(r, -1, 0)], 1) for r in rings])
    x1, y1, x2, y2 = edges[:, 0, 0], edges[:, 0, 1], edges[:, 1, 0], edges[:, 1, 1]
    for j, la in enumerate(CLAT):
        k = (y1 <= la) != (y2 <= la)
        if not k.any(): continue
        xs = np.sort(x1[k] + (la - y1[k]) * (x2[k] - x1[k]) / (y2[k] - y1[k]))
        for a, b in zip(xs[0::2], xs[1::2]):
            mask[j, (CLON >= a) & (CLON <= b)] ^= True
    return mask & ~WATER


def crop(*grids, thr=0.05):
    m = grids[0] >= thr
    if not m.any(): return None
    js, is_ = np.where(m); j0, j1, i0, i1 = max(0, js.min() - 2), min(H, js.max() + 3), max(0, is_.min() - 2), min(W, is_.max() + 3)
    return [int(j0), int(i0), int(j1 - j0), int(i1 - i0)]


def pack(grid, box, scale=100):
    if box is None: return None
    j0, i0, h, w = box
    q = np.clip(np.round(grid[j0:j0 + h, i0:i0 + w] * scale), 0, 255).astype(np.uint8)
    return base64.b64encode(q.tobytes()).decode()


def dilate(m, R):
    out = m.copy()
    for dj in range(-R, R + 1):
        for di in range(-R, R + 1):
            out |= np.roll(np.roll(m, dj, 0), di, 1)
    return out


def check(dep, pts, city, min_m=0.10):
    """share of points that fall on (or next to) a cell with depth >= min_m, and the share of the city those cells cover
    (a map that colours X% of the city would catch about X% of reports by chance)"""
    if not pts: return None
    wet = dilate(dep >= min_m, 1); n = 0
    for lon, lat in pts:
        if wet[cell(lon, lat)]: n += 1
    return dict(hit=n, n=len(pts), min_cm=round(min_m * 100), city_share=round(float((wet & city).sum() / city.sum()), 3))


def area_km2(dep, city, min_m=0.10):
    return round(float(((dep >= min_m) & city).sum()) * CELL_M * CELL * KX / 1e6, 1)


# ---------- where people are: built-up land (buildings raise the surface model above the ground estimate) ----------
def built_mask():
    raw = np.load(P('dem_raw.npy')).astype(float) / 100 if os.path.exists(P('dem_raw.npy')) else GR
    m = (raw - GR) >= 0.5                      # >= 0.5 m of buildings/trees over the ground: 57% of BKK, holds 77% of Traffy reports
    if os.path.exists(P('osm_residential.json')):
        for r in json.load(open(P('osm_residential.json'), encoding='utf-8')):
            try: m |= poly_mask([r[2]])
            except Exception: pass
    if RN:
        m |= raster_lines([w[2] for w in RN['ways']])
    return m & ~WATER
BUILT = built_mask()


def dry_sensors():
    """road sensors that read dry now (overview page data): measured beats the model on those roads"""
    if not os.path.exists(P('dashboard_data.json')): return []
    return [(r['lon'], r['lat']) for r in json.load(open(P('dashboard_data.json'), encoding='utf-8')).get('road', [])
            if r.get('lat') and r.get('status') == 'ปกติ']


def layer(key, label, dep, src, onset, city, roads_min=0.10, dry=None):
    box = crop(dep)
    j, i = cell(*SUAN_SIAM)
    roads, names = flooded_roads(dep, roads_min)
    if dry:   # drop modelled road pieces within 300 m of a sensor that reads dry
        far = lambda x, y: all(math.hypot((x - a) * KX, (y - b) * KY) > 300 for a, b in dry)
        roads = [r for r in roads if far(*r[4][len(r[4]) // 2])]
        keep = {r[0] for r in roads}; names = [n for n in names if n[0] in keep]
    return dict(key=key, label=label, box=box, depth=pack(dep, box), src=pack(src, box, 1), onset=pack(onset, box, 1),
                area10=area_km2(dep, city, 0.10), area30=area_km2(dep, city, 0.30), area50=area_km2(dep, city, 0.50),
                area10_built=area_km2(dep, city & BUILT, 0.10), area10_open=area_km2(dep, city & ~BUILT, 0.10),
                area30_built=area_km2(dep, city & BUILT, 0.30),
                suan_siam=round(float(dep[max(0, j - 1):j + 2, max(0, i - 1):i + 2].max()) * 100), roads=roads, road_names=names)


def main():
    F = json.load(open(P('forecast.json'), encoding='utf-8'))
    FR = json.load(open(P('forecast_raw.json'), encoding='utf-8'))
    M = json.load(open(P('model_data.json'), encoding='utf-8'))
    city = city_mask()
    lim, lim_pts, lim_days = drain_limit()
    t_all = FR['time']; i0 = t_all.index(F['start']) if F['start'] in t_all else 0
    hrs = F['steps'] * F['dt_h']; dt = F['dt_h']
    det = np.array(FR['det'], float)[:, i0:i0 + hrs] * float(os.environ.get('FLOOD_RAIN_X', '1'))   # test only: scale the forecast rain
    plon = [p[1] for p in FR['points']]; plat = [p[0] for p in FR['points']]
    TR = json.load(open(P('traffy_flood.json'), encoding='utf-8')) if os.path.exists(P('traffy_flood.json')) else None
    res = dict(made=datetime.now().strftime('%Y-%m-%d %H:%M'), start=F['start'], hours=hrs, fetched=M.get('fetched'),
               grid=dict(w=W, h=H, lon0=LON0, lat0=LAT0, cell=CELL),
               limit=dict(n=len(lim_pts), period=[lim_days[0], lim_days[-1]] if lim_days else None,
                          median=round(float(np.median([p[2] for p in lim_pts]))) if lim_pts else None,
                          pts=[[p[0], p[1], round(p[2])] for p in lim_pts]),
               city_km2=round(float(city.sum()) * CELL_M * CELL * KX / 1e6), layers=[], has_roads=RN is not None,
               built=base64.b64encode(np.packbits(BUILT.ravel()).tobytes()).decode())
    NONE = 255
    # ---- now: measured canal levels and flooded roads only ----
    rdep = road_now(M.get('road', []))
    dc0, _, _, vols0, _ = canal_overflow(F['scenarios'][0]['stations'], F['scenarios'][0]['districts'], use_now=True)
    dep0 = np.maximum(dc0, rdep); dep0[WATER] = 0
    src0 = np.where(dep0 < 0.05, 0, np.where(dc0 >= dep0 - 1e-9, 2, 3)).astype(float)
    L0 = layer('now', 'ตอนนี้', dep0, src0, np.where(dep0 >= 0.05, 0, NONE).astype(float), city, dry=dry_sensors())
    if TR:
        fd = datetime.strptime(TR['fetched'][:10], '%Y-%m-%d'); recent = (fd - timedelta(days=2)).strftime('%m-%d')
        rp = [(r[1], r[2]) for r in TR['rows'] if r[3] >= recent and r[4] < 4 and 100.3 < r[1] < 101 and 13.4 < r[2] < 14.1]
        L0['check'] = dict(fetched=TR['fetched'], period=f'{recent} ถึง {TR["fetched"][5:10]}', **(check(dep0, rp, city) or {}))
        L0['reports'] = [[round(x, 4), round(y, 4)] for x, y in rp]
    res['layers'].append(L0)
    # ---- next 48 h: forecast rain (+ canal forecast with the model's pumping) ----
    for sc in F['scenarios']:
        mult = F['assumptions'].get('mult', 3.0) if sc['key'] == 'extreme' else 1.0
        series = det * mult
        cum = np.concatenate([np.zeros((series.shape[0], 1)), np.cumsum(series, 1)], 1)
        win = list(range(0, max(1, hrs - 23), 1))
        r24 = np.array([[cum[p, min(hrs, h + 24)] - cum[p, h] for h in win] for p in range(series.shape[0])])   # point x window
        # rolling 24-h rain per cell, then the first window passing the local limit
        R24 = np.stack([idw(plon, plat, r24[:, k], k=len(plon)) for k in range(0, len(win), 3)], -1)
        r24max = R24.max(-1)
        over = R24 > lim[..., None]
        on_r = np.where(over.any(-1), np.minimum(hrs, np.argmax(over, -1) * 3 + 24), NONE)
        ex = np.where(WATER, 0, np.maximum(0, r24max - lim) / 1000.0)                      # m of rain the drains cannot take
        dc, on_c, used, vols, src = canal_overflow(sc['stations'], sc['districts'], rain_ex=ex, rain_on=on_r)
        dep = np.maximum(dc, rdep); dep[WATER] = 0
        src = np.where(dep < 0.05, 0, np.where(rdep >= dep - 1e-9, 3, np.maximum(src, 1))).astype(float)
        onset = np.where(src == 3, 0, np.where(dep >= 0.05, on_c, NONE)).astype(float)
        dr = np.where(src == 1, dep, 0)
        L = layer(sc['key'], sc['label'], dep, src, onset, city)
        L.update(r24max=round(float(r24max.max()), 1), rain_cells=int(((dr >= 0.10) & city).sum()), canal_vol=vols)
        res['layers'].append(L)
    # ---- hindcast: 25 Sep 2026 with the rain that fell (as if the forecast were perfect), limits from data before that day ----
    HD = '2026-09-25'
    rs = {r['code']: r for r in csv.DictReader(open(P('rain_station.csv'), encoding='utf-8-sig'))}
    rr = [(float(rs[r['code']]['lon']), float(rs[r['code']]['lat']), float(r['rf24h_mm'])) for r in csv.DictReader(open(P('rain_daily_top50.csv'), encoding='utf-8-sig'))
          if r['day'] == HD and r['rf24h_mm'] and r['code'] in rs and rs[r['code']]['lat']]
    if len(rr) >= 10 and os.environ.get('FLOOD_HINDCAST'):   # check of the method (results in NOTES), not shown on the page
        limH, ptsH, _ = drain_limit(before='2026-09-20')
        rH = idw([x[0] for x in rr], [x[1] for x in rr], [x[2] for x in rr])
        depH = excess_depth(rH, limH); depH[WATER] = 0
        L = layer('hind', 'ย้อนหลัง 25 ก.ย. 2569', depH, np.where(depH >= 0.05, 1, 0).astype(float), np.where(depH >= 0.05, 0, NONE).astype(float), city)
        L['src'] = L['onset'] = None                      # one source, no timing
        L.update(day=HD, n_rain=len(rr), rain_max=round(max(x[2] for x in rr)), rain_min=round(min(x[2] for x in rr)), n_limit=len(ptsH))
        if TR:
            tp = [(r[1], r[2]) for r in TR['rows'] if r[3][:5] in ('09-25', '09-26') and 100.3 < r[1] < 101 and 13.4 < r[2] < 14.1]
            L['check'] = dict(period='25–26 ก.ย.', **(check(depH, tp, city, 0.20) or {}))
            L['reports'] = [[round(x, 4), round(y, 4)] for x, y in tp]
        sens = {r['code']: r for r in csv.DictReader(open(P('road_sensor.csv'), encoding='utf-8-sig'))}
        se = {r['code'] for r in csv.DictReader(open(P('road_flood_event.csv'), encoding='utf-8-sig')) if r['start'][:10] in ('2026-09-25', '2026-09-26')}
        L['check_sensor'] = check(depH, [(float(sens[c]['lon']), float(sens[c]['lat'])) for c in se if c in sens], city, 0.20)
        res['layers'].append(L)
    json.dump(res, open(P('flood.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('flood.json', os.path.getsize(P('flood.json')) // 1024, 'KB')
    for L in res['layers']:
        print(' ', L['key'], 'area >=10/30/50 cm km2', L['area10'], L['area30'], L['area50'], 'suan siam cm', L['suan_siam'], 'roads', len(L['roads']),
              'check', L.get('check', {}).get('hit'), L.get('check', {}).get('n'), L.get('check', {}).get('city_share'), 'sensor', L.get('check_sensor'))


if __name__ == '__main__':
    main()
