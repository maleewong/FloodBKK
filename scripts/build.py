"""Build SQLite + CSV + dashboard data from raw pulls of weather.bangkok.go.th (DDS, BMA)."""
import json, sqlite3, csv, os, re, statistics as st
HERE = os.path.dirname(os.path.abspath(__file__)); os.chdir(os.path.join(HERE, '..', 'data'))
# run: python scripts/build.py  (reads raw JSON in data/, writes tables into data/)
from datetime import datetime, timedelta

OUT = '.'; os.makedirs(OUT, exist_ok=True)
snap = json.load(open('snapshot.json'))
wr = json.load(open('wr_raw.json'))
pump = json.load(open('pump_raw.json'))
idmap = {str(a): b for a, b in json.load(open('idmap.json'))['w']}
geo = json.load(open('bkk_districts.geojson'))
events = [e for e in json.load(open('events_raw.json')) if e[3][:4] in ('2025', '2026')] if os.path.exists('events_raw.json') else []

def utc_to_local(s):  # '2026-10-03T04:15' (UTC) -> '2026-10-03 11:15'
    if not s: return None
    return (datetime.fromisoformat(s) + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M')

def be(s):  # '01/10/2569 12:00' (Thai BE, local) -> '2026-10-01 12:00'
    d, t = (s.split(' ') + ['00:00'])[:2]
    dd, mm, yy = d.split('/'); yy = int(yy); yy = yy - 543 if yy > 2400 else yy
    return f'{yy:04d}-{int(mm):02d}-{int(dd):02d} {t}'

def num(x):
    if x in (None, '', '-'): return None
    try:
        v = float(str(x).replace(',', ''))
        return None if v <= -90 else v   # -99 = no reading
    except ValueError:
        return None

DIST = {int(f['properties']['code'][2:]): f['properties']['name'] for f in geo['features']}
STATUS_W = {'Normal': 'ปกติ', 'Alert': 'เตือนภัย', 'Critical': 'วิกฤต', 'out of order': 'ขัดข้อง', 'temporarily out of order': 'ขัดข้อง'}
STATUS_F = {'Normal': 'ปกติ', 'Minor flooding': 'ท่วมเล็กน้อย', 'Flood': 'ท่วม', 'Out of order': 'ขัดข้อง'}

db = sqlite3.connect(f'{OUT}/bkk_drainage.sqlite'); db.executescript(open(os.path.join(HERE, 'schema.sql')).read())
fetched = utc_to_local(snap['fetched'][:16])

# ---- canal / gate stations (snapshot) ----
canal = []
for x in snap['W']:
    (code, name, did, dname, lat, lon, system, gates, g1, g2, g3, ts, status, wl_in, wl_o1, wl_o2,
     lb, rb, warn, crit, warn_o1, crit_o1, max_day, max_yday, river) = x
    bank = min([b for b in (lb, rb) if b is not None], default=None)
    row = dict(code=code, name=name, district_id=did, district=dname, in_bkk=1 if 1 <= did <= 50 else 0,
               lat=lat, lon=lon, system=system, is_gate=1 if (gates or 0) > 0 or 'ปตร' in name else 0,
               river=river, left_bank=lb, right_bank=rb, warning=warn, critical=crit,
               warning_out=warn_o1, critical_out=crit_o1)
    db.execute('insert into canal_station values (:code,:name,:district_id,:district,:in_bkk,:lat,:lon,:system,:is_gate,:river,:left_bank,:right_bank,:warning,:critical,:warning_out,:critical_out)', row)
    snaprow = dict(code=code, fetched=fetched, measured=utc_to_local(ts), status=STATUS_W.get(status, status),
                   wl_in=num(wl_in), wl_out=num(wl_o1), wl_out2=num(wl_o2), gate1=g1, gate2=g2, gate3=g3,
                   max_in_today=num(max_day), max_in_yesterday=num(max_yday))
    db.execute('insert into canal_snapshot values (:code,:fetched,:measured,:status,:wl_in,:wl_out,:wl_out2,:gate1,:gate2,:gate3,:max_in_today,:max_in_yesterday)', snaprow)
    r = {**row, **snaprow}
    r['freeboard'] = round(bank - r['wl_in'], 2) if bank is not None and r['wl_in'] is not None else None
    r['head_in_out'] = round(r['wl_in'] - r['wl_out'], 2) if r['wl_in'] is not None and r['wl_out'] is not None else None
    canal.append(r)

# ---- canal hourly (last 48h) ----
hourly = {}
for sid, v in wr['water'].items():
    code = idmap.get(sid)
    if not code or not v.get('data'): continue
    pts = []
    for row in v['data']:
        ts = be(row[0]); vals = [num(c) for c in row[1:]] + [None, None, None]
        db.execute('insert or ignore into canal_hourly values (?,?,?,?,?)', (code, ts, vals[0], vals[1], vals[2]))
        pts.append([ts, vals[0], vals[1]])
    hourly[code] = sorted(pts)

# ---- road flood sensors ----
road = []; seen = set()
for x in snap['F']:
    if x[0] in seen: continue
    seen.add(x[0])
    code, fid, name, rname, did, dname, lat, lon, ts, depth, fmax, fmax_t, status, typ = x
    st_th = STATUS_F.get(status, status)
    r = dict(code=code, name=name, road=rname, district_id=did, district=dname, lat=lat, lon=lon,
             measured=utc_to_local(ts), depth_cm=depth if st_th != 'ขัดข้อง' else None, status=st_th)
    db.execute('insert into road_sensor values (?,?,?,?,?,?,?)', (code, name, rname, did, dname, lat, lon))
    db.execute('insert into road_snapshot values (?,?,?,?,?)', (code, fetched, r['measured'], r['depth_cm'], st_th))
    road.append(r)
for x in snap['T']:
    db.execute('insert into tunnel_snapshot values (?,?,?,?,?,?,?)', (x[0], x[1], x[2], x[3], fetched, utc_to_local(x[4]), x[5]))

# ---- rain stations snapshot ----
rain = []
for x in snap['R']:
    code, name, did, dname, lat, lon, ts, r15, r1, r3, r6, r12, r24, r7, r0 = x
    db.execute('insert into rain_station values (?,?,?,?,?,?)', (code, name, did, dname, lat, lon))
    db.execute('insert into rain_snapshot values (?,?,?,?,?,?,?,?,?)', (code, fetched, utc_to_local(ts), r15, r1, r3, r6, r12, r24))
    rain.append(dict(code=code, name=name, district_id=did, district=dname, lat=lat, lon=lon, rf1h=r1, rf24h=r24))

# ---- rain daily (top 50 stations per day as published) ----
rain_daily = {}
for d, code, dname, name, r24 in wr['rain']:
    day = be(d)[:10]; v = num(r24)
    db.execute('insert or ignore into rain_daily_top50 values (?,?,?,?,?)', (day, code, dname, name, v))
    rain_daily.setdefault(day, []).append(v or 0)

# ---- pump stations daily ----
pump_daily = {}; pump_station = {}; pump_rows = []
for row in pump:
    if len(row) != 6: continue
    d, _, code, name, cap, vol = row
    day = be(d)[:10]; cap, vol = num(cap), num(vol)
    db.execute('insert or ignore into pump_daily values (?,?,?,?,?)', (day, code, name, cap, vol))
    pump_daily[day] = pump_daily.get(day, 0) + (vol or 0)
    pump_rows.append((day, code, name, cap, vol))
# station totals over the last 30 days only (the page shows the period)
PUMP_DAYS = 30
_pd = sorted({r[0] for r in pump_rows})[-PUMP_DAYS:]
pump_period = [_pd[0], _pd[-1]] if _pd else [None, None]
for day, code, name, cap, vol in pump_rows:
    if day < (pump_period[0] or ''): continue
    ps = pump_station.setdefault(code, dict(code=code, name=name, cap=[], vol=0, days=0))
    if cap: ps['cap'].append(cap)
    ps['vol'] += vol or 0; ps['days'] += 1

# ---- road flood events (history) ----
ev_rows = []
for v, code, rd, s, e in events:
    try:
        t0 = datetime.fromisoformat(s); t1 = datetime.fromisoformat(e) if e else None
    except Exception:
        continue
    hrs = round((t1 - t0).total_seconds() / 3600, 2) if t1 else None
    db.execute('insert or ignore into road_flood_event values (?,?,?,?,?)', (code, rd, s, e, hrs))
    ev_rows.append((code, s, hrs, e))
db.commit()

# ---- CSV exports ----
for t in ['canal_station', 'canal_snapshot', 'canal_hourly', 'road_sensor', 'road_snapshot', 'tunnel_snapshot',
          'rain_station', 'rain_snapshot', 'rain_daily_top50', 'pump_daily', 'road_flood_event']:
    cur = db.execute(f'select * from {t}')
    with open(f'{OUT}/{t}.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f); w.writerow([c[0] for c in cur.description]); w.writerows(cur)

# ---- district summary ----
dist = {i: dict(id=i, name=DIST[i], canal=0, canal_crit=0, canal_alert=0, canal_down=0, gates=0,
                road=0, road_flood=0, road_down=0, rain24=None, rain_n=0, ev=0, ev_hours=0) for i in DIST}
for c in canal:
    d = dist.get(c['district_id'])
    if not d: continue
    d['canal'] += 1; d['gates'] += c['is_gate']
    d['canal_crit'] += c['status'] == 'วิกฤต'; d['canal_alert'] += c['status'] == 'เตือนภัย'; d['canal_down'] += c['status'] == 'ขัดข้อง'
for r in road:
    d = dist.get(r['district_id'])
    if not d: continue
    d['road'] += 1; d['road_flood'] += r['status'] in ('ท่วม', 'ท่วมเล็กน้อย'); d['road_down'] += r['status'] == 'ขัดข้อง'
for r in rain:
    d = dist.get(r['district_id'])
    if d and r['rf24h'] is not None:
        d['rain24'] = max(d['rain24'] or 0, r['rf24h']); d['rain_n'] += 1
road_by_code = {r['code']: r for r in road}
ev_by_code = {}
for code, s, hrs, e_ in ev_rows:
    e = ev_by_code.setdefault(code, dict(n=0, hours=0, long=0)); e['n'] += 1; e['hours'] += hrs or 0
    if (hrs or 0) > e['long']: e['long'] = hrs; e['long_s'] = s; e['long_e'] = e_
    r = road_by_code.get(code)
    if r and r['district_id'] in dist:
        dist[r['district_id']]['ev'] += 1; dist[r['district_id']]['ev_hours'] += hrs or 0

days = sorted(set(rain_daily) | set(pump_daily))
data = dict(
    fetched=fetched, source='สำนักการระบายน้ำ กทม. (weather.bangkok.go.th)',
    geo=geo, districts=list(dist.values()),
    canal=[{k: c[k] for k in ('code', 'name', 'district_id', 'district', 'in_bkk', 'lat', 'lon', 'is_gate', 'river', 'status', 'measured',
                              'wl_in', 'wl_out', 'gate1', 'left_bank', 'right_bank', 'warning', 'critical', 'freeboard', 'head_in_out',
                              'max_in_today', 'max_in_yesterday')} for c in canal],
    hourly=hourly, road=road, rain=rain,
    rain_daily=[[d, round(max(rain_daily[d]), 1), round(st.median(rain_daily[d]), 1)] if d in rain_daily else [d, None, None] for d in days],
    pump_daily=[[d, round(pump_daily.get(d, 0))] for d in days],
    pump_station=[dict(code=p['code'], name=p['name'], cap=round(st.median(p['cap'])) if p['cap'] else 0, vol=round(p['vol']), days=p['days'])
                  for p in pump_station.values()],
    pump_period=pump_period,
    events=[dict(code=c, **{k: (round(v, 1) if isinstance(v, (int, float)) else v) for k, v in e.items()}, name=road_by_code.get(c, {}).get('name'),
                 district=road_by_code.get(c, {}).get('district')) for c, e in ev_by_code.items()],
    events_years=sorted(set(s[:4] for _, s, _, _ in ev_rows)),
    events_range=[min(s for _, s, _, _ in ev_rows), max(e for _, _, _, e in ev_rows if e)] if ev_rows else None,
)
json.dump(data, open(f'{OUT}/dashboard_data.json', 'w'), ensure_ascii=False, separators=(',', ':'))
print('canal', len(canal), 'hourly', len(hourly), 'road', len(road), 'rain', len(rain), 'days', len(days), 'pumpst', len(pump_station), 'events', len(ev_rows))
print('json KB', os.path.getsize(f'{OUT}/dashboard_data.json') // 1024)
