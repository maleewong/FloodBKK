"""Pack the model page data: static network + calibration + the latest snapshot (stdlib only).
Canal capacities measured from the level profile (canal_profile.py) replace the width-based ones in the model.
run by update.py / render.py; writes data/model_data.json"""
import json, os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)


def num(x):
    try:
        v = float(str(x).replace(',', ''))
        return None if v <= -90 else v
    except (TypeError, ValueError):
        return None


def utc_to_local(s):
    return (datetime.fromisoformat(s[:16]) + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M') if s else None


def balance(cal):
    """rain that fell vs water actually pumped out, per day, from the data only (no model).
    rain: city-wide areal mean (published 50 wettest of 124 gauges, others at half the 50th value)
    pumped: stations with a flow meter (any non-zero day); their service area comes from calibration.json"""
    import csv, collections
    if not (os.path.exists(P('rain_daily_top50.csv')) and os.path.exists(P('pump_daily.csv'))): return None
    rd = collections.defaultdict(list)
    for r in csv.DictReader(open(P('rain_daily_top50.csv'), encoding='utf-8-sig')):
        if r['rf24h_mm']: rd[r['day']].append(float(r['rf24h_mm']))
    rows = list(csv.DictReader(open(P('pump_daily.csv'), encoding='utf-8-sig')))
    tot = collections.Counter()
    for r in rows: tot[r['code']] += float(r['discharged_m3'] or 0)
    pv = collections.defaultdict(float)
    for r in rows:
        if tot[r['code']] > 0: pv[r['day']] += float(r['discharged_m3'] or 0)
    def areal(v):
        v = sorted(v, reverse=True); return (sum(v) + (124 - len(v)) * min(v) / 2) / 124
    days = sorted(set(pv))
    return dict(A_km2=cal['A_tel_km2'], base_m3_day=cal['base_m3_day'], n_tel=sum(1 for c in tot if tot[c] > 0),
                series=[[d, round(areal(rd[d]), 1) if d in rd else 0.0, round(pv[d])] for d in days])


def main():
    net = json.load(open(P('network.json')))
    import tunnels
    tun = tunnels.apply(net)          # drainage tunnels in operation (links intake canal -> outlet pump)
    print('tunnels', [(t['name'], t['cap'], t['pump'], t['intake_m']) for t in tun])
    cal = json.load(open(P('calibration.json')))
    snap = json.load(open(P('snapshot.json')))
    geo = json.load(open(P('bkk_districts.geojson')))
    STW = {'Normal': 'ปกติ', 'Alert': 'เตือนภัย', 'Critical': 'วิกฤต', 'out of order': 'ขัดข้อง', 'temporarily out of order': 'ขัดข้อง'}

    st = {}
    for x in snap['W']:
        (code, name, did, dname, lat, lon, system, gates, g1, g2, g3, ts, status, wl_in, wl_o1, wl_o2,
         lb, rb, warn, crit, warn_o1, crit_o1, max_day, max_yday, river) = x
        st[code] = [name, num(wl_in), num(warn), num(crit), STW.get(status, status), utc_to_local(ts), river, dname,
                    lat, lon, num(wl_o1)]
    rain = {}
    for x in snap['R']:
        code, name, did, dname, lat, lon, ts, r15, r1, r3, r6, r12, r24, r7, r0 = x
        rain[code] = [name, lat, lon, num(r1), num(r3), num(r24), utc_to_local(ts)]
    road = [[x[2], x[6], x[7], x[9], x[12]] for x in snap['F'] if x[12] in ('Flood', 'Minor flooding')]

    # level along canal lines -> bottlenecks, and the capacity of the canal from the measured flow (canal_profile.py)
    import canal_profile
    gauges = {x[0]: dict(name=x[1], district=x[3], lat=x[4], lon=x[5], measured=utc_to_local(x[11]), status=STW.get(x[12], x[12]),
                         wl_in=num(x[13]), left_bank=num(x[16]), right_bank=num(x[17]), warning=num(x[18]), critical=num(x[19]))
              for x in snap['W']}
    profiles = canal_profile.compute_all(net, gauges, utc_to_local(snap['fetched']))
    cap_cal = {}
    for pr in profiles:
        if pr.get('q') and pr.get('canal'):
            q = max(10.0, min(150.0, pr['q']))
            for e in net['edges']:
                if net['names'][e['n']] == pr['canal'] if e['n'] is not None else False:
                    cap_cal.setdefault(pr['canal'], dict(before=round(e['cap0'], 1), after=round(q, 1)))
                    e['cap0'] = q
    if cap_cal: print('canal capacity from measured levels:', cap_cal)

    out = dict(
        fetched=utc_to_local(snap['fetched']),
        net=dict(names=net['names'], classes=net['classes'], V0=net['V0'],
                 nodes=[[n['lon'], n['lat'], n['A'], n['S'], n['d'], n.get('st'), n['rg']] for n in net['nodes']],
                 edges=[[e['u'], e['v'], e['n'], e['c'], e['L'], e['cap0'], e['g'], e.get('st')] for e in net['edges']],
                 pumps=net['pumps'], outlets=net['outlets'], cp=net['chao_phraya'], drained=net['drained'], source=net['source']),
        cal={k: v for k, v in cal.items()},
        balance=balance(cal),
        plan=json.load(open(P('plan.json'), encoding='utf-8')) if os.path.exists(P('plan.json')) else None,
        plan_alt=({k: v['days'] for k, v in json.load(open(P('plan_v068.json'), encoding='utf-8')).items() if isinstance(v, dict) and 'days' in v}
                  if os.path.exists(P('plan_v068.json')) else None),
        ver=json.load(open(P('verification.json'), encoding='utf-8')) if os.path.exists(P('verification.json')) else None,
        st=st, rain=rain, road=road, profiles=profiles, cap_cal=cap_cal, tunnels=tun,
        geo=[[f['properties']['code'], f['properties']['name'], f['geometry']['coordinates']] for f in geo['features']],
    )
    json.dump(out, open(P('model_data.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    print('model_data.json KB', os.path.getsize(P('model_data.json')) // 1024)


if __name__ == '__main__':
    main()
