"""Calibrate the simple model's free parameters from history (one-off; re-run after collecting more history).

Uses   data/network.json, pump_daily.csv (32 telemetry pumps, daily m3), rain_daily_top50.csv, canal_hourly.csv
Writes data/calibration.json
Needs  numpy, networkx

Fitted (assumptions we cannot measure or obtain):
  base  - dry-weather inflow (wastewater + seepage) per km2 that pumps must remove even without rain
  C     - effective runoff coefficient: share of rainfall on a pump's service area that reaches the pumps
          (same day + next day, because water is stored in canals before being pumped)
  drawdown - how fast canal levels fall after rain (m/h) -> default horizon to remove excess storage
"""
import json, csv, os, collections
import numpy as np, networkx as nx

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
net = json.load(open(P('network.json')))

# service area of each sink by shortest network distance
G = nx.Graph()
for e in net['edges']: G.add_edge(e['u'], e['v'], w=e['L'])
sinks = {}
for p in net['pumps']:
    if not p['internal']: sinks.setdefault(p['node'], p['id'])
for o in net['outlets']: sinks.setdefault(o['node'], o['id'])
_, paths = nx.multi_source_dijkstra(G, set(sinks), weight='w')
area = collections.Counter()
for n in net['nodes']:
    if n['i'] in paths: area[sinks[paths[n['i']][0]]] += n['A']
# stations whose discharge is 0 on every day are treated as not reporting (no flow meter) and left out
tot_obs = collections.Counter()
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')): tot_obs[r['code']] += float(r['discharged_m3'] or 0)
tel = [p for p in net['pumps'] if p['src'] == 'telemetry' and not p['internal'] and tot_obs[p['id']] > 0]
not_reporting = [p['name'] for p in net['pumps'] if p['src'] == 'telemetry' and not p['internal'] and tot_obs[p['id']] == 0]
tel_ids = {p['id'] for p in tel}
A_tel = sum(area[i] for i in tel_ids)

# daily areal rain (published list = 50 wettest of 124 gauges; others imputed at half the 50th value)
rd = collections.defaultdict(list)
for r in csv.DictReader(open(P('rain_daily_top50.csv'), encoding='utf-8-sig')):
    if r['rf24h_mm']: rd[r['day']].append(float(r['rf24h_mm']))
def areal(v):
    v = sorted(v, reverse=True); return (sum(v) + (124 - len(v)) * min(v) / 2) / 124
pv = collections.defaultdict(float)
for r in csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')):
    if r['code'] in tel_ids: pv[r['day']] += float(r['discharged_m3'] or 0)
days = sorted(set(rd) & set(pv))
R = np.array([areal(rd[d]) for d in days]); V = np.array([pv[d] for d in days])
X = np.column_stack([np.ones(len(days) - 1), R[1:] * A_tel * 1e3, R[:-1] * A_tel * 1e3]); y = V[1:]
coef, *_ = np.linalg.lstsq(X, y, rcond=None)
fit = X @ coef; r2 = 1 - ((y - fit) ** 2).sum() / ((y - y.mean()) ** 2).sum()
base_m3s_km2 = coef[0] / 86400 / A_tel
C = float(coef[1] + coef[2])

# pumps: how much of nameplate capacity has actually been used
pumps = []
for p in tel:
    pumps.append(dict(id=p['id'], name=p['name'], cap=p['cap'], obs_max=p['obs_max'],
                      used=round(p['obs_max'] / p['cap'], 2) if p['cap'] else None, area_km2=round(area[p['id']], 1)))
used = [p['used'] for p in pumps if p['used']]

# drawdown after rain from 48 h hourly levels
h = collections.defaultdict(list)
for r in csv.DictReader(open(P('canal_hourly.csv'), encoding='utf-8-sig')):
    if r['wl_in_m']: h[r['code']].append((r['ts'], float(r['wl_in_m'])))
rates = []
for c, v in h.items():
    w = np.array([x[1] for x in sorted(v)])
    if len(w) > 12: rates.append(-(w[6:] - w[:-6]).min() / 6)
rates = np.array(rates)

out = dict(
    period=[days[0], days[-1]], n_days=len(days),
    A_tel_km2=round(A_tel, 1), n_tel=len(tel), not_reporting=not_reporting,
    base_m3s_km2=round(base_m3s_km2, 4), base_m3_day=round(coef[0]),
    C=round(C, 3), C_same_day=round(float(coef[1]), 3), C_next_day=round(float(coef[2]), 3), r2=round(float(r2), 2),
    series=[[d, round(float(r), 1), round(float(v) / 1e6, 3), round(float(f) / 1e6, 3)] for d, r, v, f in zip(days[1:], R[1:], V[1:], fit)],
    pumps=sorted(pumps, key=lambda p: -p['cap']),
    pump_used_median=round(float(np.median(used)), 2), pump_used_p90=round(float(np.percentile(used, 90)), 2),
    drawdown_m_per_h=dict(p25=round(float(np.percentile(rates, 25)), 3), p50=round(float(np.median(rates)), 3),
                          p75=round(float(np.percentile(rates, 75)), 3), n=int(len(rates))),
)
json.dump(out, open(P('calibration.json'), 'w'), ensure_ascii=False, indent=1)
print({k: v for k, v in out.items() if k not in ('series', 'pumps')})
