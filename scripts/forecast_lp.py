"""Rain forecast for the next 48 h: which canals to pre-drain (พร่องน้ำ) before the rain, when to start the pumps,
and where the water will still rise above warning/critical levels. Time-expanded LP, 3-h steps.

State    W[i,k] water stored around node i above the lowest operating level (warning - D_LOW), split in tiers
           W1 <= S*D_LOW                (below warning, canal only)          cost 0
           W2 <= S*lam*(crit - warn)    (warning -> critical, incl. land)    cost 1 x zone weight
           W3 >= 0                      (above critical = flooding)          cost 20 x zone weight
Flows    canal f+/f- <= capacity, pump p <= cap (85%), boundary outlet o <= 5 m3/s
Inflow   C x rain x catchment (lagged 50/40/10 % over three 3-h steps) + base (dry-weather) inflow
Two runs per rain scenario: "pre-drain" (pumps free from now on) and "no pre-drain" (pumps at yesterday's rate
until the rain starts, then 85%). The difference tells which canals to lower first and by how much.

Inputs: data/forecast_raw.json (Open-Meteo, see update.py), model_data.json, plan.json (lambda, fleet), calibration.json
Output: data/forecast.json            Needs numpy, scipy (HiGHS).
"""
import json, os, csv, time, collections, math
from datetime import datetime, timedelta, timezone
import numpy as np
from scipy.sparse import coo_matrix
from scipy.optimize import linprog

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
DT_H, K = 3, 16                      # 48 h
D_LOW = 1.0                          # assumed lowest operating level = warning - 1.0 m (pre-drain limit)
NO_ST_BELOW = 0.5                    # canals without a gauge: assumed 0.5 m below warning
AVAIL, OUTLET, V_MS = 0.85, 5.0, 0.5
LAG = [0.5, 0.4, 0.1]                # share of a step's runoff reaching the canals in this / next / 2nd-next step
WEIGHT = {'ชั้นในและฝั่งตะวันตก': 3.0, 'เหนือ': 2.0, 'ตะวันออกรอบนอก': 1.0}
ZONES = {'ตะวันออกรอบนอก': {3, 11, 10, 46, 44, 32}, 'เหนือ': {42, 5, 41, 36, 43, 27}}
zone_of = lambda d: next((z for z, s in ZONES.items() if d in s), 'ชั้นในและฝั่งตะวันตก')
HEAVY_MM = 10.0                      # city-mean rain in a 3-h step that counts as heavy (pre-drain deadline)
MULT = 3.0                           # second scenario: the forecast rain x 3 (same places and timing, heavier)

M = json.load(open(P('model_data.json'), encoding='utf-8'))
PLAN = json.load(open(P('plan.json'), encoding='utf-8'))
cal = json.load(open(P('calibration.json'), encoding='utf-8'))
FC = json.load(open(P('forecast_raw.json'), encoding='utf-8'))
N = M['net']['nodes']; E = M['net']['edges']; PU = [p for p in M['net']['pumps'] if not p['internal']]; OU = M['net']['outlets']
NM = M['net']['names']; n = len(N); m = len(E); npu = len(PU); no = len(OU)
geo = {int(g[0][2:]): g[1] for g in M['geo']}
zone = [zone_of(x[4]) for x in N]
lam_z = PLAN['assumptions']['lambda_zone']; lam = np.array([lam_z.get(z, 1.0) for z in zone])
S = np.array([max(x[3], 1.0) for x in N], float)        # canal surface around the node, m2
A = np.array([x[2] for x in N], float)                   # catchment km2
C, base = cal['C'], cal['base_m3s_km2']

# ---------- initial levels (relative to warning) ----------
h0 = np.full(n, -NO_ST_BELOW); dcrit = np.full(n, 0.3); has = np.zeros(n, bool)
for i, x in enumerate(N):
    s = M['st'].get(x[5]) if x[5] else None
    if s and s[1] is not None and s[2] is not None and s[4] != 'ขัดข้อง':
        h0[i] = s[1] - s[2]; has[i] = True
        if s[3] is not None and s[3] > s[2]: dcrit[i] = s[3] - s[2]
h0 = np.maximum(h0, -D_LOW)
cap1 = S * D_LOW; cap2 = S * lam * dcrit
def W_of(h):
    return np.where(h <= 0, S * (h + D_LOW), cap1 + np.minimum(h, dcrit) * S * lam + np.maximum(0, h - dcrit) * S * lam)
def h_of(W):
    return np.where(W <= cap1, W / S - D_LOW, np.where(W <= cap1 + cap2, (W - cap1) / (S * lam), dcrit + (W - cap1 - cap2) / (S * lam)))
W0 = W_of(h0)

# ---------- rain scenarios on the node grid ----------
t_all = [datetime.fromisoformat(t) for t in FC['time']]                     # local time (Asia/Bangkok)
now = datetime.fromisoformat(FC['fetched'].replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=7))).replace(tzinfo=None)
i0 = max(0, min(len(t_all) - K * DT_H, next((k for k, t in enumerate(t_all) if t >= now.replace(minute=0, second=0, microsecond=0)), 0)))
H = K * DT_H
pts = np.array([[p[1], p[0]] for p in FC['points']])
det = np.array(FC['det'], float)[:, i0:i0 + H]                              # point x hour
ens = np.array(FC['ens'], float)[:, :, i0:i0 + H]                           # point x member x hour
kx = math.cos(math.radians(13.75))
d2 = ((np.array([x[0] for x in N])[:, None] - pts[None, :, 0]) * kx) ** 2 + (np.array([x[1] for x in N])[:, None] - pts[None, :, 1]) ** 2
wts = 1 / np.maximum(d2, 1e-6); idx = np.argsort(d2, 1)[:, 4:]; np.put_along_axis(wts, idx, 0, 1); wts /= wts.sum(1, keepdims=True)
tot_m = ens.mean(0).sum(1)                                                  # city-mean total per member
m90 = int(np.argsort(tot_m)[int(round(0.9 * (len(tot_m) - 1)))]); mmax = int(np.argmax(tot_m))
rain_pt = {'forecast': det}
# second scenario: the same forecast rain, three times heavier
rain_pt['extreme'] = det * MULT
SCEN = [('forecast', 'ฝนตามคาดการณ์', 'Open-Meteo (ECMWF / best match) 48 ชม. ข้างหน้า'),
        ('extreme', f'ฝน {MULT:.0f} เท่า', f'ฝนตามคาดการณ์ × {MULT:.0f} (ตำแหน่งและเวลาเดียวกัน)')]

# ---------- roads: rain that flooded each road sensor before (history Jul-Oct 2026) ----------
def road_thresholds():
    rain = collections.defaultdict(dict); days = set()
    for r in csv.DictReader(open(P('rain_daily_top50.csv'), encoding='utf-8-sig')):
        if not r['rf24h_mm']: continue
        d = r['day']; days.add(d); rain[d][r['district']] = max(rain[d].get(r['district'], 0.0), float(r['rf24h_mm']))
    if not days: return {}, []
    days = sorted(days); prev = {d: (datetime.fromisoformat(d) - timedelta(days=1)).strftime('%Y-%m-%d') for d in days}
    rd = lambda d, k: max(rain.get(d, {}).get(k, 0.0), rain.get(prev[d], {}).get(k, 0.0))   # same or previous day
    sens = {r['code']: r for r in csv.DictReader(open(P('road_sensor.csv'), encoding='utf-8-sig'))}
    ev = collections.defaultdict(set)
    for r in csv.DictReader(open(P('road_flood_event.csv'), encoding='utf-8-sig')):
        d = r['start'][:10]
        if days[0] <= d <= days[-1]: ev[r['code']].add(d)
    out = {}
    for c, ds in ev.items():
        s = sens.get(c)
        if not s or len(ds) < 2: continue
        v = sorted(rd(d, s['district']) for d in ds if d in prev)
        v = [x for x in v if x >= 5] or v
        if not v: continue
        t25 = v[max(0, int(0.25 * (len(v) - 1)))]; t50 = v[len(v) // 2]
        over = [d for d in days if rd(d, s['district']) >= t25]
        hit = sum(1 for d in over if d in ds or any(x in ds for x in [d]))
        out[c] = dict(code=c, name=s['name'], district=s['district'], lat=float(s['lat']), lon=float(s['lon']),
                      t25=round(t25, 1), t50=round(t50, 1), n=len(ds), p=round(hit / max(1, len(over)), 2))
    return out, days
ROAD_T, ROAD_DAYS = road_thresholds()
_nxy = np.array([[x[0] * kx, x[1]] for x in N])
road_node = {c: int(np.argmin(((_nxy[:, 0] - r['lon'] * kx) ** 2 + (_nxy[:, 1] - r['lat']) ** 2))) for c, r in ROAD_T.items()}

# ---------- pumps: yesterday's rate and plan rate ----------
pdays = collections.defaultdict(dict)
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')):
    pdays[r['day']][r['code']] = (float(r['capacity_m3'] or 0), float(r['discharged_m3'] or 0))
days3 = sorted(pdays)[-3:]; last = days3[-1]                   # average of the last 3 measured days (less noisy than one day)
_codes = {c for d in days3 for c, v in pdays[d].items() if v[1] > 0}
rep = {c: (sum(pdays[d].get(c, (0, 0))[0] for d in days3) / len(days3), sum(pdays[d].get(c, (0, 0))[1] for d in days3) / len(days3)) for c in _codes}
fleet = sum(v[1] for v in rep.values()) / max(1, sum(v[0] for v in rep.values()))
pump_measured = [[d, round(sum(pdays[d].get(c, (0, 0))[1] for c in _codes) / 1e6, 2),
                  round(sum(pdays[d].get(c, (0, 0))[1] for c in _codes) / max(1, sum(pdays[d].get(c, (0, 0))[0] for c in _codes)), 3)] for d in days3]
rate_now = np.array([rep[p['id']][1] / 86400 if p['id'] in rep else p['cap'] * fleet for p in PU])
cap_full = np.array([p['cap'] * AVAIL for p in PU])
capE = np.array([e[5] * V_MS / M['net']['V0'] for e in E])
wz = np.array([WEIGHT[z] for z in zone])

# ---------- LP ----------
NV = 2 * m + npu + no + 3 * n
off = lambda k: k * NV
def build_A():
    rows, cols, vals = [], [], []
    dt = DT_H * 3600
    for k in range(K):
        o0 = off(k); r0 = k * n
        u = np.array([e[0] for e in E]); v = np.array([e[1] for e in E]); j = np.arange(m)
        rows += [r0 + u, r0 + v, r0 + v, r0 + u]; cols += [o0 + j, o0 + j, o0 + m + j, o0 + m + j]
        vals += [np.full(m, dt), np.full(m, -dt), np.full(m, dt), np.full(m, -dt)]
        rows.append(r0 + np.array([p['node'] for p in PU])); cols.append(o0 + 2 * m + np.arange(npu)); vals.append(np.full(npu, dt))
        rows.append(r0 + np.array([o['node'] for o in OU])); cols.append(o0 + 2 * m + npu + np.arange(no)); vals.append(np.full(no, dt))
        ii = np.arange(n)
        for t in range(3):
            rows.append(r0 + ii); cols.append(o0 + 2 * m + npu + no + t * n + ii); vals.append(np.ones(n))
            if k > 0: rows.append(r0 + ii); cols.append(off(k - 1) + 2 * m + npu + no + t * n + ii); vals.append(-np.ones(n))
    rows = np.concatenate([np.atleast_1d(x) for x in rows]); cols = np.concatenate([np.atleast_1d(x) for x in cols]); vals = np.concatenate([np.atleast_1d(x) for x in vals])
    return coo_matrix((vals, (rows, cols)), shape=(n * K, NV * K)).tocsr()
A_eq = build_A()
# canal levels cannot fall faster than observed after rain (P75 of the calibration data): W[k-1] - W[k] <= S*lam*r*dt
DRAW = cal['drawdown_m_per_h']['p75']
# as variable bounds (fast): each storage tier can fall at most (k+1)*rate*dt below its starting volume
T0 = [np.minimum(W0, cap1), np.clip(W0 - cap1, 0, cap2), np.maximum(0, W0 - cap1 - cap2)]
TR = [S * DRAW * DT_H, S * lam * DRAW * DT_H, S * lam * DRAW * DT_H]

def inflow(rain_node_h):
    """m3 entering each node in each step: lagged runoff + base"""
    step = rain_node_h.reshape(n, K, DT_H).sum(2)                  # mm per step
    q = C * step / 1000 * A[:, None] * 1e6                          # m3 per step
    out = np.zeros_like(q)
    for lag, w in enumerate(LAG): out[:, lag:] += w * q[:, :K - lag]
    return out + base * A[:, None] * DT_H * 3600

def solve(rain_node_h, pump_cap_k, label):
    Q = inflow(rain_node_h)
    b = np.zeros(n * K)
    for k in range(K): b[k * n:(k + 1) * n] = Q[:, k] + (W0 if k == 0 else 0)
    # W0 split over tiers: equality uses the sum, so put W0 on the right side of step 0 only
    cost = np.zeros(NV * K); ub = np.zeros(NV * K); lb = np.zeros(NV * K)
    lenc = np.array([max(1, round(e[4] / 500)) for e in E]) * 1e-3
    for k in range(K):
        o0 = off(k)
        cost[o0:o0 + 2 * m] = np.tile(lenc, 2); ub[o0:o0 + 2 * m] = np.tile(capE, 2)
        cost[o0 + 2 * m:o0 + 2 * m + npu] = 1e-3 * (1 + 0.2 * (K - k) / K)      # prefer to pump later unless lowering early helps
        ub[o0 + 2 * m:o0 + 2 * m + npu] = pump_cap_k[k]
        cost[o0 + 2 * m + npu:o0 + 2 * m + npu + no] = 3e-3; ub[o0 + 2 * m + npu:o0 + 2 * m + npu + no] = OUTLET
        s0 = o0 + 2 * m + npu + no
        cost[s0:s0 + n] = 0; ub[s0:s0 + n] = cap1
        cost[s0 + n:s0 + 2 * n] = wz * 1e-4; ub[s0 + n:s0 + 2 * n] = cap2
        cost[s0 + 2 * n:s0 + 3 * n] = wz * 2e-3; ub[s0 + 2 * n:s0 + 3 * n] = np.inf
        for t in range(3): lb[s0 + t * n:s0 + (t + 1) * n] = np.maximum(0, T0[t] - (k + 1) * TR[t])
    t0 = time.time()
    r = linprog(cost, A_eq=A_eq, b_eq=b, bounds=np.column_stack([lb, ub]), method='highs')
    if r.status != 0: raise RuntimeError(f'{label}: {r.message}')
    x = r.x; print(f'  {label}: {time.time() - t0:.0f} s')
    W = np.array([x[off(k) + 2 * m + npu + no: off(k) + 2 * m + npu + no + n] + x[off(k) + 2 * m + npu + no + n: off(k) + 2 * m + npu + no + 2 * n]
                  + x[off(k) + 2 * m + npu + no + 2 * n: off(k) + NV] for k in range(K)])
    W2 = np.array([x[off(k) + 2 * m + npu + no + n: off(k) + 2 * m + npu + no + 2 * n] for k in range(K)])
    W3 = np.array([x[off(k) + 2 * m + npu + no + 2 * n: off(k) + NV] for k in range(K)])
    pump = np.array([x[off(k) + 2 * m: off(k) + 2 * m + npu] for k in range(K)])
    flow = np.array([x[off(k): off(k) + m] - x[off(k) + m: off(k) + 2 * m] for k in range(K)])
    return dict(W=W, W2=W2, W3=W3, pump=pump, flow=flow, h=np.array([h_of(w) for w in W]))

# ---------- run ----------
codes = sorted({x[5] for x in N if x[5]}); st_nodes = collections.defaultdict(list)
for i, x in enumerate(N):
    if x[5]: st_nodes[x[5]].append(i)
dists = sorted(set(geo.values()))
res = dict(made=time.strftime('%Y-%m-%d %H:%M'), fetched=M['fetched'], forecast_fetched=FC['fetched'], source=FC['source'],
           start=t_all[i0].strftime('%Y-%m-%dT%H:%M'), dt_h=DT_H, steps=K,
           times=[(t_all[i0] + timedelta(hours=DT_H * k)).strftime('%Y-%m-%dT%H:%M') for k in range(K + 1)],
           assumptions=dict(heavy_mm=HEAVY_MM, mult=MULT, D_LOW=D_LOW, drawdown_m_h=DRAW, no_gauge_below=NO_ST_BELOW, avail=AVAIL, outlet=OUTLET, v=V_MS, C=C, base=base, lag=LAG,
                            fleet_util_now=round(fleet, 2), lambda_zone=lam_z, pump_day=last, pump_days=days3),
           points=FC['points'], ens_totals=[round(float(v), 1) for v in sorted(tot_m)], scenarios=[])
print('rain start', res['start'], 'members', len(tot_m), 'P90 member', m90, 'max member', mmax)
for key, label, desc in SCEN:
    rn = wts @ rain_pt[key]                                               # node x hour
    city_h = (rn * A[:, None]).sum(0) / A.sum()
    step_mm = city_h.reshape(K, DT_H).sum(1)
    k_on = next((k for k, v in enumerate(step_mm) if v >= HEAVY_MM), None)   # first 3-h step with heavy rain (city mean)
    print(f'{key}: city mean {city_h.sum():.1f} mm, onset step {k_on}')
    OPT = solve(rn, [cap_full] * K, f'{key} pre-drain')
    roads = []                                   # road sensors whose 24-h rain reaches the rain that flooded them before
    for c, t in ROAD_T.items():
        h = rn[road_node[c]]; r24 = float(max(np.convolve(h, np.ones(24), 'valid'))) if len(h) >= 24 else float(h.sum())
        risk = 2 if r24 >= t['t50'] else 1 if r24 >= t['t25'] else 0
        if risk: roads.append(dict(t, r24=round(r24, 1), risk=risk))
    roads.sort(key=lambda x: (-x['risk'], -x['r24']))
    kb = K if k_on is None else k_on
    BASE = solve(rn, [rate_now if k < kb else cap_full for k in range(K)], f'{key} no pre-drain')
    def zser(X): return {z: [round(float(X[k][[i for i in range(n) if zone[i] == z]].sum()) / 1e6, 3) for k in range(K)] for z in WEIGHT}
    above = lambda R: R['W2'] + R['W3']
    # stations
    st_rows = []
    for c in codes:
        s = M['st'][c]; ii = st_nodes[c]
        if s[4] == 'ขัดข้อง' or s[1] is None or s[2] is None: continue
        ho = OPT['h'][:, ii].mean(1); hb = BASE['h'][:, ii].mean(1)
        kk = max(0, (k_on if k_on is not None else K) - 1)        # level at the end of the step before the heavy rain
        on_o, on_b = float(ho[kk]), float(hb[kk])
        st_rows.append(dict(code=c, name=s[0], district=s[7], warn=s[2], crit=s[3], now=s[1],
                            onset_opt=round(s[2] + on_o, 2), onset_base=round(s[2] + on_b, 2),
                            peak_opt=round(s[2] + float(ho.max()), 2), peak_base=round(s[2] + float(hb.max()), 2),
                            lower=round(float(s[1] - (s[2] + on_o)), 2), series=[round(s[2] + float(v), 2) for v in ho]))
        st_rows[-1]['extra'] = round(on_b - on_o, 2)                 # lowered more than "as before" by the deadline
    st_rows.sort(key=lambda r: (-r['extra'], -(r['peak_opt'] - r['crit'])))
    # pumps: schedule of the pre-drain plan
    prow = []
    for j, p in enumerate(PU):
        u = OPT['pump'][:, j]
        if u.max() < 0.5: continue
        start = next((k for k in range(K) if u[k] > rate_now[j] + max(1.0, 0.1 * p['cap'])), None)
        prow.append(dict(id=p['id'], name=p['name'], node=p['node'], cap=p['cap'], now=round(float(rate_now[j]), 1),
                         series=[round(float(v), 1) for v in u], start=start, base=[round(float(v), 1) for v in BASE['pump'][:, j]]))
    prow.sort(key=lambda r: (r['start'] if r['start'] is not None else 99, -r['cap']))
    # districts: volume above warning / critical at peak
    drow = []
    for d in dists:
        ii = [i for i in range(n) if geo.get(N[i][4]) == d]
        if not ii: continue
        a_o, a_b = above(OPT)[:, ii].sum(1), above(BASE)[:, ii].sum(1)
        c_o, c_b = OPT['W3'][:, ii].sum(1), BASE['W3'][:, ii].sum(1)
        drow.append(dict(district=d, rain=round(float((rn[ii].sum(1) * A[ii]).sum() / max(A[ii].sum(), 1e-9)), 1),
                         above_opt=[round(float(v) / 1e6, 3) for v in a_o], above_base=[round(float(v) / 1e6, 3) for v in a_b],
                         crit_opt=round(float(c_o.max()) / 1e6, 3), crit_base=round(float(c_b.max()) / 1e6, 3),
                         crit_s_opt=[round(float(v) / 1e6, 3) for v in c_o], crit_s_base=[round(float(v) / 1e6, 3) for v in c_b],
                         h_opt=[round(float(np.average(OPT['h'][k, ii], weights=S[ii])), 2) for k in range(K)],
                         h_base=[round(float(np.average(BASE['h'][k, ii], weights=S[ii])), 2) for k in range(K)]))
    # canal flows for the animation (pre-drain plan), only canals that carry water
    fl = {}
    for j in np.where(np.abs(OPT['flow']).max(0) > 0.5)[0]: fl[int(j)] = [round(float(v), 1) for v in OPT['flow'][:, j]]
    res['scenarios'].append(dict(key=key, label=label, desc=desc, city_mm=[round(float(v), 2) for v in city_h], total_mm=round(float(city_h.sum()), 1),
                                 onset=k_on, above_opt=zser(above(OPT)), above_base=zser(above(BASE)), crit_opt=zser(OPT['W3']), crit_base=zser(BASE['W3']),
                                 above0={z: round(float(sum(W0[i] - min(W0[i], cap1[i]) for i in range(n) if zone[i] == z)) / 1e6, 3) for z in WEIGHT},
                                 stations=st_rows, pumps=prow, districts=drow, flow=fl, roads=roads,
                                 pt_mm=[round(float(v), 1) for v in rain_pt[key].sum(1)],   # 48-h rain at each forecast grid point (map contours)
                                 pump_total=[round(float(v), 1) for v in OPT['pump'].sum(1)], pump_total_base=[round(float(v), 1) for v in BASE['pump'].sum(1)]))
# ---------- outlook: rain per calendar day over the whole fetched forecast (early warning beyond the 48-h plan) ----------
det_all = np.array(FC['det'], float); ens_all = np.array(FC['ens'], float)            # point x hour, point x member x hour
day0 = t_all[i0].date(); outl = collections.OrderedDict()
for h, t in enumerate(t_all):
    if h >= i0: outl.setdefault(t.date(), []).append(h)
res['outlook'] = []
for d, hs in outl.items():
    dm_ = det_all[:, hs].mean(0); em = ens_all[:, :, hs].mean(0)                       # city mean per hour (det), member x hour
    m3 = lambda v: float(max(v[j:j + 3].sum() for j in range(max(1, len(v) - 2))))
    tot_e = np.sort(em.sum(1)); max3_e = np.sort([m3(x) for x in em])
    res['outlook'].append(dict(day=d.isoformat(), hours=len(hs), det=round(float(dm_.sum()), 1), p50=round(float(tot_e[len(tot_e) // 2]), 1),
                               p90=round(float(tot_e[int(round(0.9 * (len(tot_e) - 1)))]), 1), max3_det=round(m3(dm_), 1),
                               max3_p90=round(float(max3_e[int(round(0.9 * (len(max3_e) - 1)))]), 1),
                               in_plan=bool(t_all[hs[0]] < t_all[i0] + timedelta(hours=H))))
res['road_hist'] = dict(n=len(ROAD_T), period=[ROAD_DAYS[0], ROAD_DAYS[-1]] if ROAD_DAYS else None)
json.dump(res, open(P('forecast.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('forecast.json', os.path.getsize(P('forecast.json')) // 1024, 'KB')
for s in res['scenarios']:
    print(s['key'], s['total_mm'], 'mm; peak above-warn opt/base', {z: (max(s['above_opt'][z]), max(s['above_base'][z])) for z in WEIGHT},
          'top pre-drain', [(r['name'][:25], r['lower']) for r in s['stations'][:5]])
