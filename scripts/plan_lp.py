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
import json, csv, os, collections, time, math
import numpy as np, networkx as nx
from scipy.sparse import coo_matrix
from scipy.optimize import linprog

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
DT_H, K = 24, 30                 # daily steps, 30 days
WEIGHT = {'ชั้นในและฝั่งตะวันตก': 3.0, 'เหนือ': 2.0, 'ตะวันออกรอบนอก': 1.0}   # prefer keeping water in the eastern floodway, not in the city
V_MS, AVAIL, OUTLET = float(os.environ.get('PLAN_V', 0.5)), 0.85, 5.0
ZONES = {'ตะวันออกรอบนอก': {3, 11, 10, 46, 44, 32}, 'เหนือ': {42, 5, 41, 36, 43, 27}}
zone_of = lambda d: next((z for z, s in ZONES.items() if d in s), 'ชั้นในและฝั่งตะวันตก')

M = json.load(open(P('model_data.json'), encoding='utf-8'))
cal = json.load(open(P('calibration.json'), encoding='utf-8'))
N = M['net']['nodes']; E = M['net']['edges']; PU = [p for p in M['net']['pumps'] if not p['internal']]; OU = M['net']['outlets']
n = len(N); geo = {int(g[0][2:]): g[1] for g in M['geo']}

# ---------- initial excess per node ----------
ex = np.zeros(n); S = np.array([x[3] for x in N], float); zone = [zone_of(x[4]) for x in N]
for i, x in enumerate(N):
    s = M['st'].get(x[5]) if x[5] else None
    if s and s[1] is not None and s[2] is not None and s[4] != 'ขัดข้อง': ex[i] = max(0.0, s[1] - s[2])

# ---------- observed fall of level (last 24 h) per station -> per zone ----------
hourly = collections.defaultdict(list)
for r in csv.DictReader(open(P('canal_hourly.csv'), encoding='utf-8-sig')):
    if r['wl_in_m']: hourly[r['code']].append((r['ts'], float(r['wl_in_m'])))
obs = {}
for code, s in M['st'].items():
    if s[1] is None or s[2] is None or s[4] == 'ขัดข้อง' or s[1] <= s[2]: continue
    v = [y for _, y in sorted(hourly.get(code, []))][-24:]
    if len(v) < 12: continue
    slope = np.polyfit(np.arange(len(v)), v, 1)[0] * 24          # m/day (negative = falling)
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


def solve(pump_cap, lam, label, outlet=None, capmul=None):
    """time-expanded LP; returns dict with volumes per zone per step, pump use, saturated edges"""
    m = len(E); npu = len(PU); no = len(OU)
    nv_step = 2 * m + npu + no + n          # f+, f-, p, o, V
    nvar = nv_step * K
    off = lambda k: k * nv_step
    rows, cols, vals = [], [], []; b = np.zeros(n * K)
    dt = DT_H * 3600
    V0 = S * lam * ex
    for k in range(K):
        o0 = off(k); r0 = k * n
        for j, e in enumerate(E):
            u, v = e[0], e[1]
            # f+ (u->v): leaves u, enters v ;  f- (v->u)
            rows += [r0 + u, r0 + v, r0 + v, r0 + u]; cols += [o0 + j, o0 + j, o0 + m + j, o0 + m + j]
            vals += [dt, -dt, dt, -dt]
        for j, p in enumerate(PU): rows.append(r0 + p['node']); cols.append(o0 + 2 * m + j); vals.append(dt)
        for j, o in enumerate(OU): rows.append(r0 + o['node']); cols.append(o0 + 2 * m + npu + j); vals.append(dt)
        for i in range(n):  # +V[k] - V[k-1]
            rows.append(r0 + i); cols.append(o0 + 2 * m + npu + no + i); vals.append(1.0)
            if k > 0: rows.append(r0 + i); cols.append(off(k - 1) + 2 * m + npu + no + i); vals.append(-1.0)
        if k == 0: b[r0:r0 + n] = V0
    A = coo_matrix((vals, (rows, cols)), shape=(n * K, nvar)).tocsr()
    cost = np.zeros(nvar); ub = np.zeros(nvar)
    capE = np.array([e[5] * V_MS / M['net']['V0'] * ((capmul or {}).get(M['net']['names'][e[2]], 1.0)) for e in E])
    wV = np.array([WEIGHT[z] for z in zone])
    lenc = np.array([max(1, round(e[4] / 500)) for e in E]) * 1e-3
    for k in range(K):
        o0 = off(k)
        cost[o0:o0 + m] = lenc; cost[o0 + m:o0 + 2 * m] = lenc; ub[o0:o0 + 2 * m] = np.tile(capE, 2)
        cost[o0 + 2 * m:o0 + 2 * m + npu] = 1e-3; ub[o0 + 2 * m:o0 + 2 * m + npu] = pump_cap
        cost[o0 + 2 * m + npu:o0 + 2 * m + npu + no] = 3e-3; ub[o0 + 2 * m + npu:o0 + 2 * m + npu + no] = OUTLET if outlet is None else outlet
        cost[o0 + 2 * m + npu + no:o0 + nv_step] = wV / 1e4; ub[o0 + 2 * m + npu + no:o0 + nv_step] = np.inf
    t0 = time.time()
    r = linprog(cost, A_eq=A, b_eq=b, bounds=np.column_stack([np.zeros(nvar), ub]), method='highs')
    x = r.x; print(f'{label}: status {r.status} {time.time() - t0:.0f}s vars {nvar}')
    Vk = np.array([x[off(k) + 2 * m + npu + no: off(k) + nv_step] for k in range(K)])
    pump = np.array([x[off(k) + 2 * m: off(k) + 2 * m + npu] for k in range(K)])
    outl = np.array([x[off(k) + 2 * m + npu: off(k) + 2 * m + npu + no] for k in range(K)])
    flow = np.array([x[off(k): off(k) + m] - x[off(k) + m: off(k) + 2 * m] for k in range(K)])
    zones = sorted(set(zone))
    zv = {z: [float(V0[[i for i in range(n) if zone[i] == z]].sum())] + [float(Vk[k][[i for i in range(n) if zone[i] == z]].sum()) for k in range(K)] for z in zones}
    zv['รวมทั้งเมือง'] = [float(V0.sum())] + [float(Vk[k].sum()) for k in range(K)]
    return dict(V0=V0, Vk=Vk, pump=pump, outl=outl, flow=flow, zv=zv, capE=capE)


def drain_days(series, frac=0.1):
    v0 = series[0]
    if v0 <= 0: return 0.0
    for k, v in enumerate(series):
        if v <= frac * v0: return k * DT_H / 24
    return None


# ---------- fit lambda per zone so that "current" matches the observed fall ----------
cur_cap = np.array([max(0.0, rate_now(p) - base * area[p['id']]) for p in PU])
plan_cap = np.array([max(0.0, p['cap'] * AVAIL - base * area[p['id']]) for p in PU])
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
lam_z = {z: 1.0 for z in set(zone)}
src = os.environ.get('PLAN_LAMBDA_FROM')
prev = os.path.join(DATA, src or os.environ.get('PLAN_OUT', 'plan.json'))
if os.path.exists(prev):
    try:
        for z, v in json.load(open(prev, encoding='utf-8'))['assumptions']['lambda_zone'].items():
            if z in lam_z: lam_z[z] = float(v)
        print('lambda from', os.path.basename(prev), {z: round(v, 1) for z, v in lam_z.items()})
    except Exception as e:
        print('lambda start 1.0 (', e, ')'); src = None
else:
    src = None
for it in range(0 if src else 6):
    lam = np.array([lam_z[z] for z in zone])
    R = solve(cur_cap, lam, f'current (fit {it})')
    moved = 0.0
    for z, ser in R['zv'].items():
        d = drain_days(ser, 0.5)          # time to halve (robust) vs observed time to halve = obs/2 (linear fall)
        if z in obs_days and d:
            new = max(1.0, min(200.0, lam_z[z] * (obs_days[z] / 2) / d))
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
    days = {z: {'50%': drain_days(s, 0.5), '90%': drain_days(s, 0.1)} for z, s in R['zv'].items()}
    first = slice(0, max(1, 24 // DT_H))   # first 24 h
    pumps = sorted([dict(name=PU[j]['name'], cap=PU[j]['cap'], use=round(float(R['pump'][first, j].mean()), 1),
                         util=round(float(R['pump'][first, j].mean() / PU[j]['cap']), 2) if PU[j]['cap'] else 0)
                    for j in range(len(PU))], key=lambda d: -d['use'])
    outl = sorted([dict(name=OU[j]['name'], use=round(float(R['outl'][first, j].mean()), 1)) for j in range(len(OU))], key=lambda d: -d['use'])
    act = np.abs(R['flow'][:max(1, 48 // DT_H)])     # first 2 days
    sat = (act >= 0.99 * R['capE'][None, :]) & (act > 0.3)
    nk = collections.defaultdict(lambda: dict(L=0, f=0, steps=0, d=collections.Counter()))
    for j in np.where(sat.any(0))[0]:
        nm = M['net']['names'][E[j][2]] or 'คลองไม่มีชื่อ'; g = nk[nm]
        g['L'] += E[j][4]; g['f'] = max(g['f'], float(act[:, j].max())); g['steps'] = max(g['steps'], int(sat[:, j].sum()))
        g['d'][geo.get(N[E[j][0]][4], 'นอกเขต')] += E[j][4]
    neck = sorted([dict(name=k, km=round(v['L'] / 1000, 1), q=round(v['f'], 1), district=v['d'].most_common(1)[0][0]) for k, v in nk.items()],
                  key=lambda d: -d['q'] * d['km'])[:15]
    left = collections.Counter()
    k3 = min(K - 1, int(72 / DT_H) - 1)
    k7 = min(K - 1, int(168 / DT_H) - 1)
    for i in range(n): left[geo.get(N[i][4], 'นอกเขต')] += R['Vk'][k3][i]
    return dict(days=days, series={z: [round(v / 1e6, 3) for v in s] for z, s in R['zv'].items()},
                pumps=pumps, outlets=outl[:10], bottlenecks=neck,
                left_day3=[[d, round(v / 1e6, 3)] for d, v in left.most_common(10) if v > 1e3])

# long-term test: double the conveyance of the 5 worst named bottleneck canals of the plan (changes with the data)
_neck = summary(PLAN)['bottlenecks']
TRUNK = dict(list({b['name']: 2.0 for b in _neck if b['name'] != 'คลองไม่มีชื่อ'}.items())[:5]) or {'คลองแสนแสบ': 2.0}
LONG = solve(plan_cap, lam, 'plan+trunk x2', outlet=15.0, capmul=TRUNK)

out = dict(not_reporting=cal.get('not_reporting', []), made=time.strftime('%Y-%m-%d %H:%M'), fetched=M['fetched'], dt_h=DT_H, steps=K,
           assumptions=dict(v=V_MS, avail=AVAIL, outlet=OUTLET, base=base, fleet_util_now=round(fleet, 2), pump_day=last, pump_days=days3, pump_measured=pump_measured,
                            lambda_zone={z: round(v, 1) for z, v in lam_z.items()}),
           volume0={z: round(s[0] / 1e6, 3) for z, s in CUR['zv'].items()},
           observed=dict(by_zone_days=obs_days, stations=sorted(obs.values(), key=lambda o: -(o['days'] or 999))),
           current=summary(CUR), plan=summary(PLAN), mobile=summary(MOB), trunk=summary(LONG), trunk_canals=list(TRUNK),
           plan_flow_day1=[round(float(x), 2) for x in PLAN['flow'][0]],   # canal flows of the plan, day 1 (+ = u->v), used by the ponding page
           plan_pump_day1=[round(float(x), 2) for x in PLAN['pump'][0]], pump_ids=[p['id'] for p in PU], outlet_day1=[round(float(x), 2) for x in PLAN['outl'][0]])
json.dump(out, open(P(os.environ.get('PLAN_OUT', 'plan.json')), 'w'), ensure_ascii=False, indent=1, default=float)
print(json.dumps({k: out[k] for k in ('volume0',)}, ensure_ascii=False))
for k in ('current', 'plan', 'mobile', 'trunk'): print(k, out[k]['days'])
