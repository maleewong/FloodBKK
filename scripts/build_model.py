"""Pack the model page data: static network + calibration + the latest snapshot (stdlib only).
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


def main():
    net = json.load(open(P('network.json')))
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

    out = dict(
        fetched=utc_to_local(snap['fetched']),
        net=dict(names=net['names'], classes=net['classes'], V0=net['V0'],
                 nodes=[[n['lon'], n['lat'], n['A'], n['S'], n['d'], n.get('st'), n['rg']] for n in net['nodes']],
                 edges=[[e['u'], e['v'], e['n'], e['c'], e['L'], e['cap0'], e['g'], e.get('st')] for e in net['edges']],
                 pumps=net['pumps'], outlets=net['outlets'], cp=net['chao_phraya'], drained=net['drained'], source=net['source']),
        cal={k: v for k, v in cal.items()},
        plan=json.load(open(P('plan.json'), encoding='utf-8')) if os.path.exists(P('plan.json')) else None,
        plan_alt=({k: v['days'] for k, v in json.load(open(P('plan_v068.json'), encoding='utf-8')).items() if isinstance(v, dict) and 'days' in v}
                  if os.path.exists(P('plan_v068.json')) else None),
        ver=json.load(open(P('verification.json'), encoding='utf-8')) if os.path.exists(P('verification.json')) else None,
        st=st, rain=rain, road=road,
        geo=[[f['properties']['code'], f['properties']['name'], f['geometry']['coordinates']] for f in geo['features']],
    )
    json.dump(out, open(P('model_data.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    print('model_data.json KB', os.path.getsize(P('model_data.json')) // 1024)


if __name__ == '__main__':
    main()
