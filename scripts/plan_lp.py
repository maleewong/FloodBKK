"""Multi-period drainage LP (time-expanded network) for the water that is in the canals NOW, with no further rain.

State   V[i,k]  excess volume above the warning level stored around network node i at the end of step k (m3)
Flows   f[e,k]  canal flow both directions (m3/s, <= canal capacity), p[j,k] pump j, o[j,k] boundary outlet
Balance V[i,k] = V[i,k-1] + dt*(inflow - outflow - pump - outlet)        V >= 0
Goal    minimise  sum_k sum_i V[i,k]  (drain as fast as possible)  + small costs on canal length and pumping

Pump capacity available for the excess = pump rate - base (dry-weather) load of its service area.
Scenario "current": pump rates as observed yesterday (stations without a meter: same share of capacity as the fleet)
Scenario "plan"   : pumps at `avail` of capacity, optimal routing
Effective storage: canal surface x lambda (per zone). lambda is fitted so that the "current" scenario reproduces the
water-level fall observed in the last 24 h (it absorbs flooded land beside the canals and inflow from upstream).

Needs numpy, scipy (HiGHS), networkx.   python scripts/plan_lp.py      -> data/plan.json
"""
from datetime import datetime, timedelta
import json, csv, os, collections, time, math
import numpy as np, networkx as nx
from scipy.sparse import coo_matrix
from scipy.optimize import linprog

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
DT_H, K_DAYS = 24, 30            # output: daily values for 30 days
# the LP itself: 3-hour steps for the first day (street water drains within hours), then daily steps
DTS_H = np.array([3.0] * 8 + [24.0] * (K_DAYS - 1)); K = len(DTS_H); T_END = np.cumsum(DTS_H)
DAY = [0] + [k + 1 for k in range(K) if T_END[k] % 24 == 0]          # series index at the end of each day (31 values)
FIRST = [k for k in range(K) if T_END[k] <= 24]; FIRST2 = [k for k in range(K) if T_END[k] <= 48]
dmean = lambda a, idx: (a[idx] * DTS_H[idx][:, None]).sum(0) / DTS_H[idx].sum()   # time-weighted mean over steps
WEIGHT = {'ชั้นในและฝั่งตะวันตก': 3.0, 'เหนือ': 2.0, 'ตะวันออกรอบนอก': 1.0}   # prefer keeping water in the eastern floodway, not in the city
V_MS, AVAIL, OUTLET = float(os.environ.get('PLAN_V', 0.5)), 0.85, 5.0
ZONES = {'ตะวันออกรอบนอก': {3, 11, 10, 46, 44, 32}, 'เหนือ': {42, 5, 41, 36, 43, 27}}
zone_of = lambda d: next((z for z, s in ZONES.items() if d in s), 'ชั้นในและฝั่งตะวันตก')

M = json.load(open(P('model_data.json'), encoding='utf-8'))
cal = json.load(open(P('calibration.json'), encoding='utf-8'))
N = M['net']['nodes']; E = M['net']['edges']; PU = [p for p in M['net']['pumps'] if not p['internal']]; OU = M['net']['outlets']
TUN = np.array([M['net']['names'][e[2]] in {t['name'] for t in M.get('tunnels') or []} for e in E])   # BMA tunnels (tunnels.py)
FIXED = TUN | np.array([M['net']['names'][e[2]] in (M.get('cap_cal') or {}) for e in E])
n = len(N); geo = {int(g[0][2:]): g[1] for g in M['geo']}

# ---------- initial excess per node ----------
ex = np.zeros(n); S = np.array([x[3] for x in N], float); zone = [zone_of(x[4]) for x in N]
for i, x in enumerate(N):
    s = M['st'].get(x[5]) if x[5] else None
    if s and s[1] is not None and s[2] is not None and s[4] != 'ขัดข้อง': ex[i] = max(0.0, s[1] - s[2])

# ---------- second storage layer: water outside the canals (streets, low land) that has to reach a canal first ----------
#   W_i(0) = water on streets around the flood sensors (sensor depth over the DEM cells around it, x road share)
#          + rain of the last 6 h that has not reached the canals yet (same lag as forecast_lp)
#   it drains into canal node i through the street drains: g_i <= GAMMA * A_i, and less when the canal is high:
#   g_i + kappa_i * V_i <= GAMMA * A_i, kappa_i = GAMMA * A_i / max(V_crit_i, 1.02 V_i(0)): no outflow once the canal is at its
#   critical level (where it is already above: nearly none until it falls). Lowering the canal (pumping) is what lets streets drain.
#   (the canal spilling onto streets is not modelled: the street sensors already show that water in W(0))
GAMMA_MM_H, RHO, RAD_M, DH_DEFAULT = 30.0, 0.3, 250.0, 0.3
WEIGHT_W = {'ชั้นในและฝั่งตะวันตก': 10.0, 'เหนือ': 6.0, 'ตะวันออกรอบนอก': 3.0}   # street water costs more than canal water
A_km2 = np.array([x[2] for x in N], float)
SN = json.load(open(P('snapshot.json'), encoding='utf-8'))
_num = lambda v: (lambda f: None if f is None or f < 0 else f)(float(v)) if v not in (None, '', '-') else None
_rg = {x[0]: (_num(x[9]), _num(x[10])) for x in SN.get('R', [])}            # rf3hr, rf6hr (mm)
r3 = np.zeros(n); r36 = np.zeros(n)
for i, x in enumerate(N):
    ws = [(w, _rg[c]) for c, w in (x[6] or []) if c in _rg and _rg[c][0] is not None]
    if not ws: continue
    sw = sum(w for w, _ in ws)
    r3[i] = sum(w * v[0] for w, v in ws) / sw
    r36[i] = sum(w * max(0.0, (v[1] if v[1] is not None else v[0]) - v[0]) for w, v in ws) / sw
W_rain = cal['C'] * (0.5 * r3 + 0.1 * r36) / 1000 * A_km2 * 1e6            # m3 (forecast_lp: LAG 0.5 / 0.4 / 0.1)
W_road = np.zeros(n); road_obs = []; road_skip = []
# sensor history (data/history/road_<month>.csv, hourly) to tell how the street water behaves:
#  - 'stuck'      : nearly the same small depth (<= 10 cm, range <= 1 cm) for 24 h -> sensor offset, not water (left out)
#  - 'persistent' : >= 5 cm for 12 h or more -> water standing with the canal / low land, it drains only as the canal falls
#  - 'rain'       : the rest -> rain beyond what the street drains take
import glob as _glob
def _road_hist(hours=36):
    t_snap = datetime.strptime(SN['fetched'][:16], '%Y-%m-%dT%H:%M') + timedelta(hours=7)
    h = collections.defaultdict(list)
    for fn in sorted(_glob.glob(os.path.join(DATA, 'history', 'road_????-??.csv')))[-2:]:
        for r in csv.reader(open(fn, encoding='utf-8-sig')):
            if len(r) < 3 or r[0] == 'code': continue
            try: t = datetime.strptime(r[1], '%Y-%m-%d %H:%M'); v = float(r[2])
            except ValueError: continue
            if t_snap - timedelta(hours=hours) <= t <= t_snap: h[r[0]].append((t, v))
    return h, t_snap
try: RH, T_SNAP = _road_hist()
except Exception as e: print('road history not read:', e); RH, T_SNAP = {}, None
def road_kind(code, d_cm):
    pts = sorted(RH.get(code, []))
    if T_SNAP is None or len(pts) < 6: return 'rain', 0.0
    last24 = [v for t, v in pts if t >= T_SNAP - timedelta(hours=24)]
    span = (max(t for t, _ in pts) - min(t for t, _ in pts)).total_seconds() / 3600
    if len(last24) >= 8 and span >= 12 and d_cm <= 10 and max(last24) - min(last24) <= 1.0 and min(last24) > 0: return 'stuck', 0.0
    wet_since = None
    for t, v in reversed(pts):
        if v >= 5: wet_since = t
        else: break
    wet_h = (T_SNAP - wet_since).total_seconds() / 3600 if wet_since else 0.0
    return ('persistent' if wet_h >= 12 else 'rain'), round(wet_h, 1)
try:
    DEM = np.load(P('dem_ground.npy')).astype(float) / 100.0; DM = json.load(open(P('dem_meta.json')))
    cy, cx = DM['cell'] * 110574, DM['cell'] * 110574 * math.cos(math.radians(13.75)); ca = cx * cy
    rr = int(math.ceil(RAD_M / min(cx, cy)))
    from scipy.spatial import cKDTree
    _ix = [i for i in range(n) if A_km2[i] > 0]
    TREE = cKDTree(np.array([[N[i][0] * cx / DM['cell'], N[i][1] * cy / DM['cell']] for i in _ix]))
    for f in SN.get('F', []):
        d = _num(f[9]); st = (f[12] or '').lower()
        if not d or d < 2 or 'out of order' in st or f[6] is None or f[7] is None: continue
        kind, wet_h = road_kind(f[0], d)
        if kind == 'stuck':
            road_skip.append(dict(code=f[0], name=f[2], depth_cm=round(d))); continue
        lat, lon = float(f[6]), float(f[7]); d /= 100.0
        r0, c0 = int((DM['lat0'] - lat) / DM['cell']), int((lon - DM['lon0']) / DM['cell'])
        if not (0 <= r0 < DEM.shape[0] and 0 <= c0 < DEM.shape[1]): continue
        H = DEM[r0, c0] + d; vol = 0.0
        for dr in range(-rr, rr + 1):
            for dc in range(-rr, rr + 1):
                if math.hypot(dr * cy, dc * cx) > RAD_M: continue
                r, c = r0 + dr, c0 + dc
                if 0 <= r < DEM.shape[0] and 0 <= c < DEM.shape[1] and DEM[r, c] > -50:
                    vol += ca * min(d, max(0.0, H - DEM[r, c]))
        vol *= RHO
        node = _ix[TREE.query([lon * cx / DM['cell'], lat * cy / DM['cell']])[1]]
        W_road[node] += vol
        road_obs.append(dict(code=f[0], name=f[2], road=f[3], district=f[5], lat=lat, lon=lon, depth_cm=round(d * 100), vol_m3=round(vol), node=node,
                             kind=kind, wet_h=wet_h))
except Exception as e:
    print('street water from flood sensors skipped:', e)
# storages outside the canals: one per node for rain still arriving (drains of the whole catchment of the node),
# one per flooded sensor for the street water around it (drains of the flooded area only: pi R^2)
JW = [i for i in range(n) if W_rain[i] > 50.0]; WV0 = [float(W_rain[i]) for i in JW]; WA = [float(A_km2[i]) for i in JW]
WP = []          # storages of standing water (kind 'persistent')
for r in road_obs:
    r['wq'] = len(JW); JW.append(r['node']); WV0.append(float(r['vol_m3'])); WA.append(math.pi * RAD_M ** 2 / 1e6)
    if r['kind'] == 'persistent': WP.append(r['wq'])
if os.environ.get('PLAN_LAMBDA_FROM'):     # sensitivity run (only the canal speed changes): canal layer only, keeps the hour short
    JW, WV0, WA, WP, road_obs = [], [], [], [], []
WV0 = np.array(WV0); WA = np.array(WA); WP = set(WP)
print(f'water outside canals: rain still arriving {W_rain.sum() / 1e6:.3f} Mm3, on streets near {len(road_obs)} flooded sensors '
      f'{W_road.sum() / 1e6:.3f} Mm3, {len(JW)} storages')

# ---------- observed fall of level (last 24 h) per station -> per zone ----------
hourly = collections.defaultdict(list)
for r in csv.DictReader(open(P('canal_hourly.csv'), encoding='utf-8-sig')):
    if r['wl_in_m']: hourly[r['code']].append((r['ts'], float(r['wl_in_m'])))
obs = {}
for code, s in M['st'].items():
    if s[1] is None or s[2] is None or s[4] == 'ขัดข้อง' or s[1] <= s[2]: continue
    pts = sorted(hourly.get(code, []))
    # the latest run of hourly readings without gaps (> 3 h) that has at least 12 hours; its last 24 h
    runs, cur = [], []
    for t, y in pts:
        tt = datetime.strptime(t, '%Y-%m-%d %H:%M')
        if cur and (tt - cur[-1][0]).total_seconds() > 3 * 3600: runs.append(cur); cur = []
        cur.append((tt, y))
    if cur: runs.append(cur)
    run = next((r for r in reversed(runs) if len(r) >= 12), None)
    if not run: continue
    t_last = run[-1][0]; pts = [(t.strftime('%Y-%m-%d %H:%M'), y) for t, y in run if t >= t_last - timedelta(hours=24)]
    if len(pts) < 12: continue
    hrs = [(datetime.strptime(t, '%Y-%m-%d %H:%M') - t_last).total_seconds() / 3600 for t, _ in pts]
    slope = np.polyfit(hrs, [y for _, y in pts], 1)[0] * 24      # m/day (negative = falling), by real time
    obs[code] = dict(name=s[0], district=s[7], ex=round(s[1] - s[2], 2), fall_cm_day=round(-slope * 100, 1),
                     days=round((s[1] - s[2]) / -slope, 1) if slope < -0.002 else None)

# ---------- service areas (nearest sink by canal distance) and base load per pump ----------
G = nx.Graph()
for k, e in enumerate(E): G.add_edge(e[0], e[1], w=e[4])
sink_nodes = {p['node']: ('P', p['id']) for p in PU}
for o in OU: sink_nodes.setdefault(o['node'], ('O', o['id']))
_, paths = nx.multi_source_dijkstra(G, set(sink_nodes), weight='w')
area = collections.Counter()
for i in range(n):
    if i in paths: area[sink_nodes[paths[i][0]][1]] += N[i][2]
base = cal['base_m3s_km2']

# current pumping (yesterday)
pdays = collections.defaultdict(dict)
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')):
    pdays[r['day']][r['code']] = (float(r['capacity_m3'] or 0), float(r['discharged_m3'] or 0))
days3 = sorted(pdays)[-3:]; last = days3[-1]                   # average of the last 3 measured days (less noisy than one day)
_codes = {c for d in days3 for c, v in pdays[d].items() if v[1] > 0}
rep = {c: (sum(pdays[d].get(c, (0, 0))[0] for d in days3) / len(days3), sum(pdays[d].get(c, (0, 0))[1] for d in days3) / len(days3)) for c in _codes}
fleet = sum(v[1] for v in rep.values()) / max(1, sum(v[0] for v in rep.values()))
pump_measured = [[d, round(sum(pdays[d].get(c, (0, 0))[1] for c in _codes) / 1e6, 2),
                  round(sum(pdays[d].get(c, (0, 0))[1] for c in _codes) / max(1, sum(pdays[d].get(c, (0, 0))[0] for c in _codes)), 3)] for d in days3]
def rate_now(p):
    return rep[p['id']][1] / 86400 if p['id'] in rep else p['cap'] * fleet


# canal reach of each node = the gauge nearest along the canals: the street drains of a node are blocked by the level of
# its reach (the water of the whole reach), not by the node alone (the LP could otherwise just move it next door)
_gauged = {i for i in range(n) if N[i][5] in M['st'] and M['st'][N[i][5]][1] is not None and M['st'][N[i][5]][2] is not None
           and M['st'][N[i][5]][4] != 'ขัดข้อง' and i in G}
_, _gp = nx.multi_source_dijkstra(G, _gauged, weight='w') if _gauged else ({}, {})
GRP_NODES = sorted(_gauged); gidx = {g: q for q, g in enumerate(GRP_NODES)}
grp = np.array([gidx[_gp[i][0]] if i in _gp else -1 for i in range(n)])


def solve(pump_cap, lam, label, outlet=None, capmul=None):
    """time-expanded LP with two storage layers (canal V, outside the canals W); returns volumes per zone per step,
    pump use, saturated edges. Step k lasts DTS_H[k] hours."""
    m = len(E); npu = len(PU); no = len(OU); nw = len(JW); ng = len(GRP_NODES)
    zones_ = sorted(set(zone)); zidx = {z: q for q, z in enumerate(zones_)}; nz = len(zones_)
    oV = 2 * m + npu + no; oW = oV + n; oG = oW + nw; oU = oG + nw; oZ = oU + ng
    nv_step = oZ + nz                       # f+, f-, p, o, V, W, g, U (volume of each reach), Z (volume of each zone)
    nvar = nv_step * K
    off = lambda k: k * nv_step
    rows, cols, vals = [], [], []; nr = n + nw + ng + nz; b = np.zeros(nr * K)
    ub_rows, ub_cols, ub_vals, ub_b = [], [], [], []
    V0 = S * lam * ex
    dh = np.array([max(0.1, (M['st'][N[i][5]][3] - M['st'][N[i][5]][2])) if N[i][5] in M['st'] and M['st'][N[i][5]][3] is not None
                   and M['st'][N[i][5]][2] is not None else DH_DEFAULT for i in range(n)])
    gmax = GAMMA_MM_H / 1000 / 3600 * WA * 1e6
    # drains stop when the canal reaches its critical level; where it is already above, they stay (almost) shut until it falls
    Vc = lam * np.maximum(S, 1000.0) * dh                 # node volume at the critical level
    # the excess of a reach starts at its gauge node (V0 = lam S ex there), so its critical volume is that of the gauge node
    Uc = np.array([Vc[g] for g in GRP_NODES]); U0 = np.zeros(ng)
    for i in range(n):
        if grp[i] >= 0: U0[grp[i]] += V0[i]
    kap = np.array([gmax[q] / max(1.0, Uc[grp[i]], 1.02 * U0[grp[i]]) if grp[i] >= 0 else 0.0 for q, i in enumerate(JW)])
    # standing water that is level with the canal: shut now, opens in proportion as the reach falls (block = U / U0)
    # (the reach must be clearly above its warning level; otherwise the standing water is a local low spot or blocked
    #  drains: it has not drained for 12 h with the canal low, so the model does not let it drain: pumping does not help)
    # standing water level with a high canal falls only as the whole canal system of the zone falls (the canals are one
    # connected water body: a reach that empties into the next one does not lower the water): g + (gmax / Z0) Z_zone <= gmax
    Z0 = np.zeros(nz)
    for i in range(n): Z0[zidx[zone[i]]] += V0[i]
    pmode = {}; zcon = {}
    for q in WP:
        i = JW[q]; g_ = grp[i]
        if g_ >= 0 and U0[g_] >= 0.25 * Uc[g_] and Z0[zidx[zone[i]]] > 1.0:
            kap[q] = gmax[q] / Z0[zidx[zone[i]]]; pmode[q] = 'canal'; zcon[q] = zidx[zone[i]]
        else: gmax[q] = 0.0; kap[q] = 0.0; pmode[q] = 'local'
    for k in range(K):
        o0 = off(k); r0 = k * nr; dt = DTS_H[k] * 3600
        for j, e in enumerate(E):
            u, v = e[0], e[1]
            rows += [r0 + u, r0 + v, r0 + v, r0 + u]; cols += [o0 + j, o0 + j, o0 + m + j, o0 + m + j]
            vals += [dt, -dt, dt, -dt]
        for j, p in enumerate(PU): rows.append(r0 + p['node']); cols.append(o0 + 2 * m + j); vals.append(dt)
        for j, o in enumerate(OU): rows.append(r0 + o['node']); cols.append(o0 + 2 * m + npu + j); vals.append(dt)
        for i in range(n):  # +V[k] - V[k-1]
            rows.append(r0 + i); cols.append(o0 + oV + i); vals.append(1.0)
            if k > 0: rows.append(r0 + i); cols.append(off(k - 1) + oV + i); vals.append(-1.0)
        for q, i in enumerate(JW):   # W[k] - W[k-1] + dt g = 0 ;  g enters canal node i
            rows.append(r0 + n + q); cols.append(o0 + oW + q); vals.append(1.0)
            if k > 0: rows.append(r0 + n + q); cols.append(off(k - 1) + oW + q); vals.append(-1.0)
            rows.append(r0 + n + q); cols.append(o0 + oG + q); vals.append(dt)
            rows.append(r0 + i); cols.append(o0 + oG + q); vals.append(-dt)
            # street drains are slower when the canal reach is high: g + kappa U_reach <= gmax
            if q in zcon:
                ub_rows += [len(ub_b), len(ub_b)]; ub_cols += [o0 + oG + q, o0 + oZ + zcon[q]]; ub_vals += [1.0, kap[q]]; ub_b.append(gmax[q])
            elif grp[i] >= 0:
                ub_rows += [len(ub_b), len(ub_b)]; ub_cols += [o0 + oG + q, o0 + oU + grp[i]]; ub_vals += [1.0, kap[q]]; ub_b.append(gmax[q])
        for i in range(n):   # U_g - sum of V in the reach = 0
            if grp[i] >= 0: rows.append(r0 + n + nw + grp[i]); cols.append(o0 + oV + i); vals.append(-1.0)
        for g_ in range(ng): rows.append(r0 + n + nw + g_); cols.append(o0 + oU + g_); vals.append(1.0)
        for i in range(n): rows.append(r0 + n + nw + ng + zidx[zone[i]]); cols.append(o0 + oV + i); vals.append(-1.0)
        for z_ in range(nz): rows.append(r0 + n + nw + ng + z_); cols.append(o0 + oZ + z_); vals.append(1.0)
        if k == 0:
            b[r0:r0 + n] = V0; b[r0 + n:r0 + n + nw] = WV0
    A = coo_matrix((vals, (rows, cols)), shape=(nr * K, nvar)).tocsr()
    Aub = coo_matrix((ub_vals, (ub_rows, ub_cols)), shape=(len(ub_b), nvar)).tocsr() if ub_b else None
    cost = np.zeros(nvar); ub = np.zeros(nvar)
    # tunnels (pump-limited, one way intake -> outlet) and canals whose capacity comes from measured levels keep their capacity
    # when the assumed flow speed changes; the rest scale with V_MS
    capE = np.array([e[5] if FIXED[k] else e[5] * V_MS / M['net']['V0'] * ((capmul or {}).get(M['net']['names'][e[2]], 1.0)) for k, e in enumerate(E)])
    capR = np.where(TUN, 0.0, capE)
    wV = np.array([WEIGHT[z] for z in zone]); wW = np.array([WEIGHT_W[zone[i]] for i in JW])
    lenc = np.array([max(1, round(e[4] / 500)) for e in E]) * 1e-3
    for k in range(K):
        o0 = off(k); tw = DTS_H[k] / 24          # stored volume is costed per day it stays
        cost[o0:o0 + m] = lenc; cost[o0 + m:o0 + 2 * m] = lenc; ub[o0:o0 + m] = capE; ub[o0 + m:o0 + 2 * m] = capR
        cost[o0 + 2 * m:o0 + 2 * m + npu] = 1e-3; ub[o0 + 2 * m:o0 + 2 * m + npu] = pump_cap
        cost[o0 + 2 * m + npu:o0 + oV] = 3e-3; ub[o0 + 2 * m + npu:o0 + oV] = OUTLET if outlet is None else outlet
        cost[o0 + oV:o0 + oW] = wV / 1e4 * tw; ub[o0 + oV:o0 + oW] = np.inf
        cost[o0 + oW:o0 + oG] = wW / 1e4 * tw; ub[o0 + oW:o0 + oG] = np.inf
        ub[o0 + oG:o0 + oU] = gmax; ub[o0 + oU:o0 + nv_step] = np.inf
    lb = np.zeros(nvar)
    t0 = time.time()
    r = linprog(cost, A_eq=A, b_eq=b, A_ub=Aub, b_ub=np.array(ub_b) if ub_b else None,
                bounds=np.column_stack([lb, ub]), method='highs')
    x = r.x; print(f'{label}: status {r.status} {time.time() - t0:.0f}s vars {nvar}')
    Vk = np.array([x[off(k) + oV: off(k) + oW] for k in range(K)])
    Wk = np.array([x[off(k) + oW: off(k) + oG] for k in range(K)]) if nw else np.zeros((K, 0))
    Gk = np.array([x[off(k) + oG: off(k) + oU] for k in range(K)]) if nw else np.zeros((K, 0))
    Uk = np.array([x[off(k) + oU: off(k) + oZ] for k in range(K)])
    Zk = np.array([x[off(k) + oZ: off(k) + nv_step] for k in range(K)])
    pump = np.array([x[off(k) + 2 * m: off(k) + 2 * m + npu] for k in range(K)])
    outl = np.array([x[off(k) + 2 * m + npu: off(k) + oV] for k in range(K)])
    flow = np.array([x[off(k): off(k) + m] - x[off(k) + m: off(k) + 2 * m] for k in range(K)])
    zones = sorted(set(zone))
    zi = {z: [i for i in range(n) if zone[i] == z] for z in zones}
    zv = {z: [float(V0[zi[z]].sum())] + [float(Vk[k][zi[z]].sum()) for k in range(K)] for z in zones}
    zv['รวมทั้งเมือง'] = [float(V0.sum())] + [float(Vk[k].sum()) for k in range(K)]
    zq = {z: [q for q, i in enumerate(JW) if zone[i] == z] for z in zones}
    zw = {z: [float(WV0[zq[z]].sum())] + [float(Wk[k][zq[z]].sum()) for k in range(K)] for z in zones}
    zw['รวมทั้งเมือง'] = [float(WV0.sum())] + [float(Wk[k].sum()) for k in range(K)]
    gq = np.array([max(grp[i], 0) for i in JW], int)
    block = (kap * Uk[:, gq]) / np.maximum(gmax, 1e-9) if nw else np.zeros((K, 0))   # share of the drain capacity lost to a high canal
    for q, z_ in zcon.items(): block[:, q] = Zk[:, z_] / max(Z0[z_], 1e-9)
    for q, md in pmode.items():
        if md == 'local': block[:, q] = 1.0
    return dict(V0=V0, Vk=Vk, Wk=Wk, Gk=Gk, block=block, pmode=pmode, pump=pump, outl=outl, flow=flow, zv=zv, zw=zw, capE=capE)


def drain_days(series, frac=0.1):
    """days until the series (index 0 = start, k = end of step k-1) falls to frac of its start"""
    v0 = series[0]
    if v0 <= 0: return 0.0
    for k, v in enumerate(series):
        if v <= frac * v0: return 0.0 if k == 0 else round(float(T_END[k - 1]) / 24, 2)
    return None


# ---------- fit lambda per zone so that "current" matches the observed fall ----------
cur_cap = np.array([max(0.0, rate_now(p) - base * area[p['id']]) for p in PU])
plan_cap = np.array([max(0.0, p['cap'] * p.get('avail', AVAIL) - base * area[p['id']]) for p in PU])   # avail: from the pump records (build_model)
obs_zone = collections.defaultdict(list)
st_zone = {}
for i, x in enumerate(N):
    if x[5]: st_zone[x[5]] = zone[i]
for c, o in obs.items():
    if c in st_zone and o['days']: obs_zone[st_zone[c]].append(o['days'])
obs_days = {z: float(np.median(v)) for z, v in obs_zone.items()}
print('observed median days to warning level', obs_days)

# PLAN_LAMBDA_FROM=plan.json: take lambda from that run, no fitting (sensitivity runs: only the canal speed changes)
# otherwise start from the lambda of the last run (it changes slowly hour to hour) and stop once it settles
# storage multiplier bounds: the eastern floodway stores water on its fields; the built-up inner city and north do not
# (there a slow fall after rain is more rain arriving, not storage). Each fit step changes lambda at most x2 / x0.5.
LAM_MAX = {'ตะวันออกรอบนอก': 200.0, 'เหนือ': 5.0, 'ชั้นในและฝั่งตะวันตก': 5.0}
lam_z = {z: 1.0 for z in set(zone)}
src = os.environ.get('PLAN_LAMBDA_FROM')
prev = os.path.join(DATA, src or os.environ.get('PLAN_OUT', 'plan.json'))
if os.path.exists(prev):
    try:
        for z, v in json.load(open(prev, encoding='utf-8'))['assumptions']['lambda_zone'].items():
            if z in lam_z: lam_z[z] = min(LAM_MAX.get(z, 5.0), float(v))
        print('lambda from', os.path.basename(prev), {z: round(v, 1) for z, v in lam_z.items()})
    except Exception as e:
        print('lambda start 1.0 (', e, ')'); src = None
else:
    src = None
# lambda is only re-fitted from a dry recession. With rain in the last 24 h (the window of the observed fall) the fall is
# street runoff arriving or draining, not stored water, and the fit swings (5-6 Oct 2026: east 124 -> 1.1 on the VPS,
# -> 195 on the laptop for the same hours). Then the zone keeps the lambda of the last run. Otherwise one run may move
# lambda at most x1.5 / x(1/1.5) from the last run (the steps inside one run are still at most x2 / x0.5).
RAIN24_MM, RAIN6_MM, RISING_SHARE, RUN_STEP = 5.0, 10.0, 0.4, 1.5
lam_prev = dict(lam_z); lam_status = {z: 'fit' for z in lam_z}
try:
    SN = json.load(open(P('snapshot.json'), encoding='utf-8'))
    rz = collections.defaultdict(list)
    for r in SN.get('R', []):
        if r[12] is not None and r[2] is not None: rz[zone_of(r[2])].append((float(r[12]), float(r[10] or 0)))
except Exception as e:
    print('rain for lambda check not read:', e); rz = {}
rising = collections.defaultdict(lambda: [0, 0])
for c, o in obs.items():
    if c in st_zone:
        rising[st_zone[c]][1] += 1; rising[st_zone[c]][0] += o['fall_cm_day'] < 0
if not src and os.path.exists(prev):
    for z in lam_z:
        r24 = float(np.mean([a for a, _ in rz[z]])) if rz.get(z) else 0.0
        r6 = max([b for _, b in rz[z]], default=0.0) if rz.get(z) else 0.0
        up, nst = rising[z]
        if r24 >= RAIN24_MM or r6 >= RAIN6_MM:
            lam_status[z] = f'คงค่าเดิม: ฝน 24 ชม. เฉลี่ย {r24:.0f} มม. (สูงสุด 6 ชม. {r6:.0f} มม.)'
        elif nst and up / nst >= RISING_SHARE:
            lam_status[z] = f'คงค่าเดิม: น้ำกำลังขึ้น {up}/{nst} สถานี'
    print('lambda status', lam_status)
fit_z = [z for z in lam_z if lam_status[z] == 'fit']
for it in range(0 if (src or not fit_z) else 6):
    lam = np.array([lam_z[z] for z in zone])
    R = solve(cur_cap, lam, f'current (fit {it})')
    moved = 0.0
    for z, ser in R['zv'].items():
        d = drain_days(ser, 0.5)          # time to halve (robust) vs observed time to halve = obs/2 (linear fall)
        if z in obs_days and d and z in fit_z:
            new = max(1.0, min(LAM_MAX.get(z, 5.0), lam_z[z] * min(2.0, max(0.5, (obs_days[z] / 2) / d))))
            if not src and os.path.exists(prev):     # at most x1.5 per run from the last run
                new = min(lam_prev[z] * RUN_STEP, max(lam_prev[z] / RUN_STEP, new))
            moved = max(moved, abs(new / lam_z[z] - 1)); lam_z[z] = new
    print('lambda', {z: round(v, 1) for z, v in lam_z.items()})
    if moved < 0.05: break                # settled: the next fit would give the same drain days
lam = np.array([lam_z[z] for z in zone])
CUR = solve(cur_cap, lam, 'current')
PLAN = solve(plan_cap, lam, 'plan')
# short-term add-on: mobile pumps raise each southern/eastern boundary outlet to 15 m3/s
MOB = solve(plan_cap, lam, 'plan+mobile', outlet=15.0)

# ---------- summaries ----------
def summary(R):
    r1 = lambda v: None if v is None else round(v, 1)
    days = {z: {'50%': r1(drain_days(s, 0.5)), '90%': r1(drain_days(s, 0.1))} for z, s in R['zv'].items()}
    pu1 = dmean(R['pump'], FIRST); ou1 = dmean(R['outl'], FIRST)      # first 24 h, time-weighted
    pumps = sorted([dict(name=PU[j]['name'], cap=PU[j]['cap'], use=round(float(pu1[j]), 1),
                         util=round(float(pu1[j] / PU[j]['cap']), 2) if PU[j]['cap'] else 0)
                    for j in range(len(PU))], key=lambda d: -d['use'])
    outl = sorted([dict(name=OU[j]['name'], use=round(float(ou1[j]), 1)) for j in range(len(OU))], key=lambda d: -d['use'])
    act = np.abs(R['flow'][FIRST2])     # first 2 days
    sat = (act >= 0.99 * R['capE'][None, :]) & (act > 0.3)
    nk = collections.defaultdict(lambda: dict(L=0, f=0, steps=0, d=collections.Counter()))
    for j in np.where(sat.any(0))[0]:
        nm = M['net']['names'][E[j][2]] or 'คลองไม่มีชื่อ'; g = nk[nm]
        g['L'] += E[j][4]; g['f'] = max(g['f'], float(act[:, j].max())); g['steps'] = max(g['steps'], int(sat[:, j].sum()))
        g['d'][geo.get(N[E[j][0]][4], 'นอกเขต')] += E[j][4]
    neck = sorted([dict(name=k, km=round(v['L'] / 1000, 1), q=round(v['f'], 1), district=v['d'].most_common(1)[0][0]) for k, v in nk.items()],
                  key=lambda d: -d['q'] * d['km'])[:15]
    left = collections.Counter()
    k3 = int(np.searchsorted(T_END, 72))
    for i in range(n): left[geo.get(N[i][4], 'นอกเขต')] += R['Vk'][k3][i]
    return dict(days=days, series={z: [round(s[i] / 1e6, 3) for i in DAY] for z, s in R['zv'].items()},
                surface={z: [round(s[i] / 1e6, 4) for i in range(10)] for z, s in R['zw'].items()},   # 0,3,...,24,48 h
                pumps=pumps, outlets=outl[:10], bottlenecks=neck,
                left_day3=[[d, round(v / 1e6, 3)] for d, v in left.most_common(10) if v > 1e3])

# long-term test: double the conveyance of the 5 worst named bottleneck canals of the plan (changes with the data)
_neck = summary(PLAN)['bottlenecks']
TRUNK = dict(list({b['name']: 2.0 for b in _neck if b['name'] != 'คลองไม่มีชื่อ'}.items())[:5]) or {'คลองแสนแสบ': 2.0}
LONG = solve(plan_cap, lam, 'plan+trunk x2', outlet=15.0, capmul=TRUNK)

out = dict(not_reporting=cal.get('not_reporting', []), made=time.strftime('%Y-%m-%d %H:%M'), fetched=M['fetched'], dt_h=DT_H, steps=K_DAYS,
           assumptions=dict(v=V_MS, avail=AVAIL, outlet=OUTLET, base=base, fleet_util_now=round(fleet, 2), pump_day=last, pump_days=days3, pump_measured=pump_measured,
                            lambda_zone={z: round(v, 1) for z, v in lam_z.items()}, lambda_status=lam_status),
           volume0={z: round(s[0] / 1e6, 3) for z, s in CUR['zv'].items()},
           observed=dict(by_zone_days=obs_days, stations=sorted(obs.values(), key=lambda o: -(o['days'] or 999))),
           current=summary(CUR), plan=summary(PLAN), mobile=summary(MOB), trunk=summary(LONG), trunk_canals=list(TRUNK),
           plan_flow_day1=[round(float(x), 2) for x in dmean(PLAN['flow'], FIRST)],   # canal flows of the plan, day 1 (+ = u->v), used by the ponding page
           plan_pump_day1=[round(float(x), 2) for x in dmean(PLAN['pump'], FIRST)], pump_ids=[p['id'] for p in PU],
           outlet_day1=[round(float(x), 2) for x in dmean(PLAN['outl'], FIRST)])
out['pump_rate_measured'] = {c: round(v[1] / 86400, 1) for c, v in rep.items()}   # m3/s, mean of pump_days (telemetry)
# tunnels: daily flow (m3/s, + = intake -> outlet) per scenario and the volume moved in 30 days (million m3)
out['tunnels'] = [dict(name=M['net']['names'][E[k][2]], cap=float(E[k][5]),
                       **{sc: dict(q=[round(float(np.vstack([dmean(R['flow'], FIRST)[None], R['flow'][len(FIRST):]])[d, k]), 1) for d in range(K_DAYS)],
                                   mm3=round(float((R['flow'][:, k].clip(min=0) * DTS_H).sum()) * 3600 / 1e6, 1))
                          for sc, R in (('current', CUR), ('plan', PLAN), ('mobile', MOB))})
                  for k in np.flatnonzero(TUN)]
# ---------- water outside the canals: what the pumps are for ----------
# each flooded sensor -> its canal node -> the sink of that node (pump or outlet, nearest by canal distance)
def clear_h(R, q, frac=0.1):
    w0 = WV0[q]
    if w0 <= 0: return 0.0
    prev_w, prev_t = w0, 0.0
    for k in range(K):
        w = R['Wk'][k, q]
        if w <= frac * w0:           # linear within the step
            return round(prev_t + (prev_w - frac * w0) / max(prev_w - w, 1e-9) * (T_END[k] - prev_t), 1)
        prev_w, prev_t = w, float(T_END[k])
    return None
pname = {p['id']: p['name'] for p in PU}; oname = {o['id']: o['name'] for o in OU}
sink_of = lambda i: sink_nodes[paths[i][0]] if i in paths else (None, None)
roads = []
for r in road_obs:
    q = r.get('wq')
    if q is None: continue
    kind, sid = sink_of(r['node'])
    kind = r['kind'] if r['kind'] != 'persistent' else 'persistent_' + CUR['pmode'].get(q, 'local')
    roads.append(dict({k: v for k, v in r.items() if k not in ('wq', 'kind')}, kind=kind, node=int(r['node']), zone=zone[r['node']], sink=(pname if kind == 'P' else oname).get(sid, '') if kind else '',
                      sink_id=sid, canal_excess_m=round(float(ex[r['node']]), 2),
                      block_now=round(float(CUR['block'][0, q]), 2), block_plan=round(float(PLAN['block'][0, q]), 2),
                      clear_h_current=clear_h(CUR, q), clear_h_plan=clear_h(PLAN, q)))
roads.sort(key=lambda d: -d['depth_cm'])
# pumps and boundary outlets that the plan runs harder than today, and the flooded streets in their service area
use1 = {}
for R_, key in ((CUR, 'cur'), (PLAN, 'plan')):
    pu, ou = dmean(R_['pump'], FIRST), dmean(R_['outl'], FIRST)
    for j, p in enumerate(PU): use1.setdefault(p['name'], {})[key] = float(pu[j])
    for j, o in enumerate(OU): use1.setdefault(o['name'], {})[key] = float(ou[j])
purpose = []
for nm, u in use1.items():
    add = u.get('plan', 0.0) - u.get('cur', 0.0)
    rs = [r for r in roads if r['sink'] == nm]
    if add >= 1.0 or rs:
        purpose.append(dict(name=nm, add_m3s=round(add, 1), use=round(u.get('plan', 0.0), 1), streets=[r['code'] for r in rs],
                            street_m3=round(sum(r['vol_m3'] for r in rs)),
                            faster_h=round(sum(((r['clear_h_current'] or 48) - (r['clear_h_plan'] or 48)) for r in rs), 1)))
purpose.sort(key=lambda d: (-d['faster_h'], -len(d['streets']), -d['add_m3s']))
out['surface'] = dict(
    t_h=[0.0] + [float(t) for t in T_END[:9]],
    assumptions=dict(gamma_mm_h=GAMMA_MM_H, road_share=RHO, radius_m=RAD_M, C=cal['C'], rain_lag='0.5 x ฝน 3 ชม. + 0.1 x ฝน 3-6 ชม.',
                     weight=WEIGHT_W, dh_default=DH_DEFAULT),
    volume0=dict(rain_mm3=round(float(W_rain.sum()) / 1e6, 4), streets_mm3=round(float(W_road.sum()) / 1e6, 4), nodes=len(JW),
                 by_zone={z: round(float(sum(WV0[q] for q, i in enumerate(JW) if zone[i] == z)) / 1e6, 4) for z in sorted(set(zone))}),
    roads=roads[:60], purpose=purpose[:25], skipped=road_skip)
print(f"street water: {len(roads)} flooded sensors, plan clears them in "
      f"{np.nanmedian([r['clear_h_plan'] or np.nan for r in roads]) if roads else 0} h (median), current {np.nanmedian([r['clear_h_current'] or np.nan for r in roads]) if roads else 0} h")
json.dump(out, open(P(os.environ.get('PLAN_OUT', 'plan.json')), 'w'), ensure_ascii=False, indent=1, default=float)
print(json.dumps({k: out[k] for k in ('volume0',)}, ensure_ascii=False))
for k in ('current', 'plan', 'mobile', 'trunk'): print(k, out[k]['days'])
