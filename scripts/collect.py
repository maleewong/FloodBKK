"""Collect-only run (no models, no web pages): every 10 minutes on the VPS.

    python scripts/collect.py            BMA sensors (canals, roads, rain, tunnels) -> data/history/hires/
                                         + hourly pump volumes (BMA) + hourly Chao Phraya levels/discharge (RID)
    python scripts/collect.py --test     parse checks only (no download)

Why: the hourly model run keeps one reading per station per hour. Road flooding rises and falls within minutes, so
for research the sensors are also kept every 10 minutes (one request per map, the same 3 requests as the hourly run).
Writes only its own files, so it can run at the same time as update.py:
    data/history/hires/<kind>_<YYYY-MM-DD>.csv.gz   canal / road / rain / tunnel every 10 min (gzip, appended)
    data/history/pump_hourly_<YYYY-MM>.csv           pumped volume per station and hour (BMA WaterPumpHistory)
    data/history/river_<YYYY-MM>.csv                 RID hourly water level + discharge: Chao Phraya, Pasak, Tha Chin
    data/history/river_stations.csv                  RID stations (bank level, discharge at bank-full)
Stdlib only.
"""
import collections, csv, gzip, io, json, os, re, sys, time
import urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import update as U          # get(), append_csv(), ms_to_utc(), table_rows(), HIST

TH = timezone(timedelta(hours=7))
now_th = lambda: datetime.now(TH).replace(tzinfo=None)
loc = lambda u: (datetime.fromisoformat(u) + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M') if u else None
def num(s):
    """'1,234.5' -> 1234.5; missing-value marks of the sites ('', '-', '*', '**', 'N/A' ...) -> None"""
    if s is None or isinstance(s, (int, float)): return s
    try: return float(str(s).replace(',', '').strip())
    except ValueError: return None


# ---------------------------------------------------------------- 10-minute sensor readings
def gz_append(fn, head, rows, key=2):
    """append rows to a gzip CSV (a new gzip member each time; readers see one file), skipping keys already there"""
    seen = set()
    if os.path.exists(fn):
        try:
            with gzip.open(fn, 'rt', encoding='utf-8') as f:
                seen = {tuple(r[:key]) for r in csv.reader(f)}
        except (OSError, EOFError):          # cut-off file (power loss): keep it aside, start again
            os.replace(fn, fn + '.bad')
    nz = lambda x: '' if x is None else str(x)
    new = [[nz(x) for x in r] for r in rows if r[1] and tuple(nz(x) for x in r[:key]) not in seen]
    if not new: return 0
    buf = io.StringIO(); w = csv.writer(buf)
    if not seen: w.writerow(head)
    w.writerows(new)
    with gzip.open(fn, 'at', encoding='utf-8') as f: f.write(buf.getvalue())
    return len(new)


def hires():
    """the three BMA map requests (same as update.snapshot) -> one row per station and reading time"""
    W = json.loads(U.get('/water/PageMap/GoogleMap', b''))
    F = json.loads(U.get('/Flood/PageMap/GetData?id=0'))
    R = json.loads(U.get('/rain/PageMap/GetDataForUpdate', b''))
    extra = {f.get('flood_code'): f for f in (F.get('floodTbl') or [])}
    kinds = dict(
        canal=(['code', 'measured', 'wl_in_m', 'wl_out_m', 'wl_out2_m', 'gate1', 'gate2', 'gate3', 'status'],
               [(w['water_code'], loc(U.ms_to_utc(w.get('site_timestamp'))), w.get('wl_in'), w.get('wl_out01'), w.get('wl_out02'),
                 w.get('watergate01'), w.get('watergate02'), w.get('watergate03'), w.get('txtStatus_en')) for w in W]),
        road=(['code', 'measured', 'depth_cm', 'max_cm', 'status'], []),
        rain=(['code', 'measured', 'rf15m_mm', 'rf1h_mm', 'rf3h_mm', 'rf6h_mm', 'rf12h_mm', 'rf24h_mm'],
              [(r['rain_code'], loc(U.ms_to_utc(r.get('site_timestamp'))), r.get('rf15min'), r.get('rf1hr'), r.get('rf3hr'),
                r.get('rf6hr'), r.get('rf12hr'), r.get('rf24hr')) for r in R]),
        tunnel=(['code', 'measured', 'depth_cm', 'max_cm'],
                [(t['flood_code'], loc(U.ms_to_utc(t.get('site_timestamp') or t.get('site_timestatmpTH'))), t.get('flood'), t.get('flood_max'))
                 for t in F.get('dtTblTunel') or []]))
    for f0 in F.get('dtTbl') or []:
        f = {**extra.get(f0.get('flood_code'), {}), **{k: v for k, v in f0.items() if v is not None}}
        kinds['road'][1].append((f['flood_code'], loc(U.ms_to_utc(f.get('site_timestamp') or f.get('site_timestatmpTH'))),
                                 f.get('flood'), f.get('flood_max'), f.get('chkStatustxt_en') or 'Normal'))
    os.makedirs(U.HIST('hires'), exist_ok=True)
    out = {}
    for k, (head, rows) in kinds.items():
        by = collections.defaultdict(list)
        for r in rows:
            if r[1]: by[r[1][:10]].append(r)
        out[k] = sum(gz_append(U.HIST('hires', f'{k}_{d}.csv.gz'), head, rr) for d, rr in by.items())
    return out


# ---------------------------------------------------------------- hourly pump volumes (BMA)
PUMP_HEAD = ['code', 'hour', 'name', 'capacity_m3', 'discharged_m3']


def pump_hour(day, h):
    """pumped volume of every telemetry pump station during one hour ('HH:00'-'HH:59') of `day` (date)"""
    s = day.strftime('%d/%m/%Y')
    rows = U.table_rows(U.get('/water/WaterPumpHistory', dict(waterIdField='A1', dateStart=s, dateEnd=s,
                                                             TimeStart=f'{h:02d}:00', timeEnd=f'{h:02d}:59'), timeout=90))
    t = f'{day:%Y-%m-%d} {h:02d}:00'; seen = set(); out = []
    for r in rows:
        if len(r) == 5 and r[1].startswith('WL.') and r[1] not in seen:
            seen.add(r[1]); out.append((r[1], t, r[2], num(r[3]), num(r[4])))
    return out


def pump_hourly(max_req=6, back_h=48, lag_h=2):
    """fill the hours of the last `back_h` hours that are not saved yet (an hour is read `lag_h` hours after it ends);
    at most `max_req` requests per run. data/history/pump_hourly_done.txt lists the hours already read."""
    done_fn = U.HIST('pump_hourly_done.txt')
    done = set(open(done_fn).read().split('\n')) if os.path.exists(done_fn) else set()
    end = now_th().replace(minute=0, second=0, microsecond=0) - timedelta(hours=lag_h)
    todo = [end - timedelta(hours=k) for k in range(1, back_h + 1)]
    todo = [t for t in todo if f'{t:%Y-%m-%d %H}' not in done][:max_req]
    n = 0
    for t in todo:
        rows = pump_hour(t.date(), t.hour)
        if not rows: continue
        n += U.append_csv(U.HIST(f'pump_hourly_{t:%Y-%m}.csv'), PUMP_HEAD, rows)
        with open(done_fn, 'a') as f: f.write(f'{t:%Y-%m-%d %H}\n')
        time.sleep(1)
    return len(todo), n


# ---------------------------------------------------------------- RID hourly river levels and discharge
RID = 'https://hyd-app-db.rid.go.th'
RID_BASINS = [(5, 10, 'เจ้าพระยา'), (5, 12, 'ป่าสัก'), (7, 13, 'ท่าจีน')]   # (hydrology centre, basin)
RIVER_HEAD = ['code', 'measured', 'wl_msl_m', 'q_m3s', 'stage_m']
RST_HEAD = ['code', 'name', 'province', 'basin', 'bank_m', 'zero_gauge_m', 'q_bankfull_m3s', 'first_seen']


def rid_post(path, data, js=True):
    body = json.dumps(data).encode() if js else urllib.parse.urlencode(data).encode()
    hd = {'User-Agent': U.UA['User-Agent'], 'Content-Type': 'application/json; charset=utf-8' if js else 'application/x-www-form-urlencoded'}
    for k in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(RID + path, data=body, headers=hd, method='POST'), timeout=30, context=U.CTX) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            if k == 1: raise
            time.sleep(5)


def rid_parse(col, data, day, basin):
    """col: GetColModelAllHL answer, data: getGroupHourlyWaterLevelReportAllHL answer, for one day.
    returns (stations, readings). Stations at the lower Chao Phraya report m MSL directly; others report a stage on a
    gauge whose zero is 'ZG+x ม.(รสม.)' (MSL = stage + zero)."""
    lab = {c['name']: c.get('label') or '' for c in col.get('colModel', [])}
    st = []; meta = {}
    for i, g in enumerate(col.get('groupHeaders') or [], 1):
        m = re.match(r'\s*(\S+)\s+(.*?)\s*$', g['titleText'])
        code, rest = (m[1], m[2]) if m else (g['titleText'], '')
        prov = rest.split()[-1] if rest.split() else ''
        name = rest[:len(rest) - len(prov)].strip()
        wl = lab.get(f'wlvalues{i}', ''); q = lab.get(f'qvalues{i}', '')
        bank = re.search(r'ระดับตลิ่ง\s*(-?[\d.]+)', wl); zg = re.search(r'ZG\s*([+-]\s*[\d.]+)', wl); qb = re.search(r'ปริมาณ\s*([\d.]+)', q)
        z = float(zg[1].replace(' ', '')) if zg else None
        meta[i] = (code, z)
        st.append((code, name, prov, basin, float(bank[1]) if bank else None, z, float(qb[1]) if qb else None, None))
    out = []
    for r in data.get('rows') or []:
        try: h = float(r.get('hourlytime'))
        except (TypeError, ValueError): continue
        t = datetime(day.year, day.month, day.day) + timedelta(hours=h)      # '24.00' -> next day 00:00
        for i, (code, z) in meta.items():
            v = num(r.get(f'wlvalues{i}')); q = num(r.get(f'qvalues{i}'))
            if v is None and q is None: continue
            msl = None if v is None else round(v + z, 3) if z is not None else round(v, 3)
            out.append((code, t.strftime('%Y-%m-%d %H:%M'), msl, q, round(v, 3) if (v is not None and z is not None) else None))
    return st, out


def rid_day(day):
    be = f'{day:%d/%m}/{day.year + 543}'; st_all = []; rows_all = []
    for u, b, name in RID_BASINS:
        col = rid_post('/webservice/HDService.svc/GetColModelAllHL', {'hydro': dict(UtokID=u, BasinID=b, TimeCurrent=be)})
        data = rid_post('/webservice/getGroupHourlyWaterLevelReportAllHL.ashx',
                        {'DW[UtokID]': u, 'DW[BasinID]': b, 'DW[TimeCurrent]': be, '_search': 'false', 'rows': '100', 'page': '1',
                         'sidx': 'indexhourly', 'sord': 'asc'}, js=False)
        st, rows = rid_parse(col, data, day, name)
        st_all += st; rows_all += rows; time.sleep(1)
    return st_all, rows_all


def save_river(st, rows):
    stamp = now_th().strftime('%Y-%m-%d %H:%M')
    U.append_csv(U.HIST('river_stations.csv'), RST_HEAD, [s[:7] + (stamp,) for s in st], key=7)
    by = collections.defaultdict(list)
    for r in rows: by[r[1][:7]].append(r)
    return sum(U.append_csv(U.HIST(f'river_{m}.csv'), RIVER_HEAD, rr) for m, rr in by.items())


def river(every_min=55, after_fail_h=6):
    """today's table every hour (it grows through the day); yesterday's once more after midnight to complete it.
    The RID site does not answer some data-centre addresses (the VPS, 5 Oct 2026): after a failure wait `after_fail_h`
    hours. Missed days are not lost: the laptop fetches them (fetch_history.py --kinds river) and sends them here."""
    fn = U.HIST('river_last.txt'); t = now_th()
    last, ok = (open(fn).read().split() + ['', ''])[:2] if os.path.exists(fn) else ('', '')
    if last:
        wait = every_min * 60 if ok != 'fail' else after_fail_h * 3600
        if (t - datetime.strptime(last.replace('_', ' '), '%Y-%m-%d %H:%M')).total_seconds() < wait: return None
    days = [t.date()] + ([t.date() - timedelta(days=1)] if t.hour < 3 or not last else [])
    stamp = t.strftime('%Y-%m-%d_%H:%M')
    open(fn, 'w').write(stamp + ' fail')
    n = 0
    for d in days:
        st, rows = rid_day(d); n += save_river(st, rows)
    open(fn, 'w').write(stamp + ' ok')
    return n


def rid_report():
    """RID daily Chao Phraya summary (C.2, Chao Phraya dam, C.29B Bang Sai, Pasak dam) as published, once a day
    after 10:00 -> data/history/rid_daily/YYYY-MM-DD.pdf (kept as is; the RID site that answers the VPS)"""
    t = now_th()
    out = U.HIST('rid_daily', f'{t:%Y-%m-%d}.pdf')
    if t.hour < 10 or os.path.exists(out): return None
    with urllib.request.urlopen(urllib.request.Request('https://water.rid.go.th/flood/flood/daily.pdf', headers={'User-Agent': U.UA['User-Agent']}),
                                timeout=60, context=U.CTX) as r:
        pdf = r.read()
    if not pdf.startswith(b'%PDF'): return 'not a pdf'
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, 'wb').write(pdf)
    return f'{len(pdf) // 1024} KB'


# ---------------------------------------------------------------- self-test (no network)
def _test():
    col = {'colModel': [{'name': 'wlvalues1', 'label': 'ระดับตลิ่ง 3.00 ม.'}, {'name': 'qvalues1', 'label': 'ปริมาณ 3600.00 ลบ.ม./วินาที'},
                        {'name': 'wlvalues2', 'label': 'ระดับตลิ่ง 11.20 ม. ZG-9.700 ม.(รสม.)'}, {'name': 'qvalues2', 'label': 'ปริมาณ  ลบ.ม./วินาที'}],
           'groupHeaders': [{'titleText': 'C.29B วัดกร่าง ปทุมธานี'}, {'titleText': 'T.1 ที่ว่าการอ.นครชัยศรี นครปฐม'}]}
    data = {'rows': [{'hourlytime': '1.00', 'wlvalues1': 2.7699999809, 'qvalues1': '2104.00', 'wlvalues2': 10.5, 'qvalues2': '**'},
                     {'hourlytime': '24.00', 'wlvalues1': 2.8, 'qvalues1': '**', 'wlvalues2': None, 'qvalues2': '**'}]}
    st, rows = rid_parse(col, data, datetime(2026, 10, 5).date(), 'เจ้าพระยา')
    assert st[0][:7] == ('C.29B', 'วัดกร่าง', 'ปทุมธานี', 'เจ้าพระยา', 3.0, None, 3600.0), st[0]
    assert st[1][5] == -9.7 and st[1][6] is None, st[1]
    assert rows[0] == ('C.29B', '2026-10-05 01:00', 2.77, 2104.0, None), rows[0]
    assert rows[1] == ('T.1', '2026-10-05 01:00', 0.8, None, 10.5), rows[1]
    assert rows[2] == ('C.29B', '2026-10-06 00:00', 2.8, None, None), rows[2]
    import tempfile
    fn = os.path.join(tempfile.mkdtemp(), 'x.csv.gz')
    assert gz_append(fn, ['code', 'measured', 'v'], [('A', '2026-10-05 10:00', 1), ('B', '2026-10-05 10:00', None)]) == 2
    assert gz_append(fn, ['code', 'measured', 'v'], [('A', '2026-10-05 10:00', 1), ('A', '2026-10-05 10:10', 2)]) == 1
    with gzip.open(fn, 'rt', encoding='utf-8') as f: got = list(csv.reader(f))
    assert got == [['code', 'measured', 'v'], ['A', '2026-10-05 10:00', '1'], ['B', '2026-10-05 10:00', ''], ['A', '2026-10-05 10:10', '2']], got
    print('collect.py self-test OK')


if __name__ == '__main__':
    if '--test' in sys.argv: _test(); sys.exit(0)
    t0 = time.time(); msg = []
    for name, fn in (('ค่าตรวจวัดทุก 10 นาที', hires), ('ปริมาณสูบรายชั่วโมง', pump_hourly), ('ระดับน้ำ/น้ำไหล กรมชลประทาน', river),
                     ('รายงานกรมชลฯ รายวัน', rid_report)):
        try: msg.append(f'{name}: {fn()}')
        except Exception as e: msg.append(f'{name}: ไม่สำเร็จ ({e})')
    print(f'[{now_th():%Y-%m-%d %H:%M}] ' + ' · '.join(msg) + f' ({time.time() - t0:.0f} s)')
