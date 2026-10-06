"""Download past measurements that the BMA and RID websites keep (for research: earlier rainy seasons, gaps).

    python scripts/fetch_history.py                         this rainy season (1 May to yesterday), all kinds
    python scripts/fetch_history.py --from 2025-05-01 --to 2025-11-30
    python scripts/fetch_history.py --kinds canal,pump      only some kinds: canal rain pump river
    python scripts/fetch_history.py --hours 6               stop after 6 hours (run again later: it continues)
    python scripts/fetch_history.py --kinds river --days 10 the last 10 days only
On the VPS it runs every night at 01:40 for up to 4 hours (floodbkk-history.timer): the first nights fill this season,
afterwards each night adds the days that are complete (canal and rain in whole 3-day blocks).

What it fetches (checked 5 Oct 2026: BMA keeps 5-minute data back to at least 2023, RID hourly back to 2025 and earlier):
    canal  5-minute canal levels inside/outside every gate, per station, 3 days per request (BMA /water/WaterHistory)
    rain   5-minute rain per gauge, 3 days per request (BMA /rain/RainHistory)
    pump   pumped volume per telemetry pump station and hour (BMA /water/WaterPumpHistory, one request per hour)
    river  hourly water level and discharge, Chao Phraya / Pasak / Tha Chin (RID hydro, one day per request)
Output (gzip CSV, one file per kind and day, appended without duplicates):
    data/history/past/canal5m_<YYYY-MM-DD>.csv.gz  code, measured, wl_in_m, wl_out_m, wl_out2_m
    data/history/past/rain5m_<YYYY-MM-DD>.csv.gz   code, measured, rf5m_mm, rf15m_mm, rf30m_mm, rf1h_mm, rf3h_mm, rf6h_mm, rf12h_mm, rf24h_mm
    pump and river go to the same monthly files as the live collection (pump_hourly_<YYYY-MM>.csv, river_<YYYY-MM>.csv)
One request at a time with a pause between requests (public websites: keep the load low). Progress is kept in
data/history/past/done.txt, so a stopped run continues where it stopped.
"""
import collections, os, re, sys, time
from datetime import datetime, timedelta, date

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import update as U
import collect as C

PAUSE = 2.0
PAST = lambda *a: U.HIST('past', *a)


def be(d): return f'{d:%d/%m/%Y}'          # the BMA history forms take the Christian year


def to_iso(s):
    m = re.match(r'\s*(\d+)/(\d+)/(\d{4})\s+(\d+):(\d+)', s or '')
    if not m: return None
    y = int(m[3]); y -= 543 if y > 2400 else 0
    return f'{y:04d}-{int(m[2]):02d}-{int(m[1]):02d} {int(m[4]):02d}:{m[5]}'


def options(path, name):
    page = U.get(path)
    sel = re.search(rf'<select[^>]*name="{name}"[\s\S]*?</select>', page)
    return [(v, re.sub(r'\s+', ' ', t).strip()) for v, t in re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)', sel[0] if sel else '') if v not in ('', '0')]


def save_days(prefix, head, rows):
    by = collections.defaultdict(list)
    for r in rows: by[r[1][:10]].append(r)
    return sum(C.gz_append(PAST(f'{prefix}_{d}.csv.gz'), head, rr) for d, rr in by.items())


def canal(sid, d1, d2):
    rows = U.table_rows(U.get('/water/WaterHistory', dict(water_station=sid, datePick_start=be(d1), StationTime_start='00:00',
                                                           rain_field_selected='0', rain_data='0', txtFilter='0',
                                                           datePick_end=be(d2), StationTime_end='23:55'), timeout=120))
    out = [(r[0], to_iso(r[3]), C.num(r[4]), C.num(r[5]), C.num(r[6])) for r in rows if len(r) >= 7 and r[0].startswith('WL.') and to_iso(r[3])]
    return save_days('canal5m', ['code', 'measured', 'wl_in_m', 'wl_out_m', 'wl_out2_m'], out), len(out)


def rain(sid, d1, d2):
    rows = U.table_rows(U.get('/rain/RainHistory', dict(rain_station=sid, datePick_start=be(d1), StationTime_start='00:00',
                                                         rain_field_selected='0', rain_data='1', txtFilter='0',
                                                         datePick_end=be(d2), StationTime_end='23:55'), timeout=120))
    out = [(r[0], to_iso(r[3]), *[C.num(x) for x in r[4:12]]) for r in rows if len(r) >= 12 and r[0].startswith('RF.') and to_iso(r[3])]
    return save_days('rain5m', ['code', 'measured', 'rf5m_mm', 'rf15m_mm', 'rf30m_mm', 'rf1h_mm', 'rf3h_mm', 'rf6h_mm', 'rf12h_mm', 'rf24h_mm'],
                     out), len(out)


def main():
    a = sys.argv[1:]
    arg = lambda k, dflt: a[a.index(k) + 1] if k in a else dflt
    y = date.today() - timedelta(days=1)
    d0 = datetime.strptime(arg('--from', f'{y.year}-05-01'), '%Y-%m-%d').date()
    if '--days' in a: d0 = y - timedelta(days=int(arg('--days', '7')) - 1)
    d1 = datetime.strptime(arg('--to', str(y)), '%Y-%m-%d').date()
    kinds = arg('--kinds', 'river,pump,canal,rain').split(',')
    stop = time.time() + float(arg('--hours', '24')) * 3600
    os.makedirs(PAST(), exist_ok=True)
    done_fn = PAST('done.txt')
    done = set(open(done_fn, encoding='utf-8').read().split('\n')) if os.path.exists(done_fn) else set()
    mark = lambda key: (done.add(key), open(done_fn, 'a', encoding='utf-8').write(key + '\n'))
    days = [d0 + timedelta(days=k) for k in range((d1 - d0).days + 1)]
    win = [days[i:i + 3] for i in range(0, len(days), 3)]                # 3 days per request (the sites' limit)
    if '--to' not in a and win and len(win[-1]) < 3: win = win[:-1]       # nightly run: whole windows only (the rest next time)
    print(f'ดึงข้อมูลย้อนหลัง {d0} ถึง {d1}: {", ".join(kinds)}')
    for kind in kinds:
        if kind == 'river':
            todo = [d for d in days if f'river|{d}' not in done]
            items = [(f'river|{d}', lambda d=d: C.save_river(*C.rid_day(d))) for d in todo]
        elif kind == 'pump':
            items = [(f'pump|{d}|{h:02d}', lambda d=d, h=h: U.append_csv(U.HIST(f'pump_hourly_{d:%Y-%m}.csv'), C.PUMP_HEAD, C.pump_hour(d, h)))
                     for d in days for h in range(24) if f'pump|{d}|{h:02d}' not in done]
        elif kind in ('canal', 'rain'):
            st = options('/water/WaterHistory', 'water_station') if kind == 'canal' else options('/rain/RainHistory', 'rain_station')
            print(f'  {kind}: {len(st)} สถานี')
            fn = canal if kind == 'canal' else rain
            items = [(f'{kind}|{sid}|{w[0]}|{w[-1]}', lambda sid=sid, w=w: fn(sid, w[0], w[-1])[0])
                     for w in win for sid, _ in st if f'{kind}|{sid}|{w[0]}|{w[-1]}' not in done]
        else:
            print('ไม่รู้จักชนิด', kind); continue
        print(f'  {kind}: เหลือ {len(items)} คำขอ (ราว {len(items) * (PAUSE + 3) / 3600:.1f} ชม.)')
        n = 0; t0 = time.time()
        for k, (key, job) in enumerate(items):
            if time.time() > stop:
                print('ครบเวลาที่กำหนด หยุดไว้ก่อน (สั่งใหม่จะทำต่อ)'); return
            try:
                n += job() or 0; mark(key)
            except Exception as e:
                print(f'    {key}: ไม่สำเร็จ ({e}) ข้ามไปก่อน'); time.sleep(10)
            if k % 50 == 49: print(f'    {kind} {k + 1}/{len(items)} · +{n} แถว · {(time.time() - t0) / 60:.0f} นาที')
            time.sleep(PAUSE)
        print(f'  {kind}: เสร็จ +{n} แถว')


if __name__ == '__main__':
    main()
