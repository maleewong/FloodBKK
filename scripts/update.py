"""Fetch the latest data from the BMA Drainage Department website and rebuild both pages.

    python scripts/update.py             current snapshot (canals, roads, rain, tunnels) + pump volumes for missing days
    python scripts/update.py --hourly    also refresh 48 h hourly canal levels (≈ 270 requests, 2–10 min)
    python scripts/update.py --no-pump   snapshot only (fastest, ~10 s)
    python scripts/update.py --offline   rebuild pages from data already on disk (no download)
    python scripts/update.py --no-forecast  skip the 48-h rain forecast (Open-Meteo)
    python scripts/update.py --no-plan   skip the 30-day drainage plan (multi-period LP, needs numpy scipy networkx)
    The plan fits its storage factor to the last 24 h of hourly levels: use --hourly with it for an up-to-date plan.

Only the Python standard library is needed. The site is a public web page, not an official API: requests are
kept slow and few. Run from anywhere; paths are relative to this file.
"""
import collections, csv, json, os, re, sys, time, subprocess, ssl, html
import urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, '..'); DATA = os.path.join(ROOT, 'data')
P = lambda f: os.path.join(DATA, f)
BASE = 'https://weather.bangkok.go.th'
UA = {'User-Agent': 'Mozilla/5.0 (FloodBKK research prototype)', 'X-Requested-With': 'XMLHttpRequest'}
CTX = ssl.create_default_context()


def get(path, data=None, timeout=60, tries=3):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    for k in range(tries):
        try:
            req = urllib.request.Request(BASE + path, data=body, headers=UA, method='POST' if body is not None else 'GET')
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:
            if k == tries - 1: raise
            print(f'  retry {path} ({e})'); time.sleep(3 * (k + 1))


def ms_to_utc(s):
    """site time -> UTC 'YYYY-MM-DDTHH:MM'. Accepts '/Date(ms)/', a local ISO time '2026-10-05T18:05:00' or '05/10/2569 18:05'"""
    m = re.search(r'/Date\((-?\d+)\)/', s or '')
    if m: return datetime.fromtimestamp(int(m[1]) / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M')
    m = re.match(r'(\d{4})-(\d+)-(\d+)[T ](\d+):(\d+)', s or '')
    if m: return (datetime(*map(int, m.groups())) - timedelta(hours=7)).strftime('%Y-%m-%dT%H:%M')
    return th_to_utc(s)


def th_to_utc(s):
    """'26/09/2569 13:45' (Bangkok time, Buddhist year) -> '2026-09-26T06:45' (UTC)"""
    m = re.match(r'\s*(\d+)/(\d+)/(\d{4})\s+(\d+):(\d+)', s or '')
    if not m: return None
    y = int(m[3]); y -= 543 if y > 2400 else 0
    t = datetime(y, int(m[2]), int(m[1]), int(m[4]), int(m[5])) - timedelta(hours=7)
    return t.strftime('%Y-%m-%dT%H:%M')


def append_csv(fn, head, rows, key=2):
    """append rows to a CSV, skipping rows whose first `key` columns are already in it; a file with an older
    header is kept as <name>_v1.csv and a new file is started. On Linux the file is locked while this runs, so the
    hourly run, the 10-minute collection and the nightly history download can write the same file safely."""
    try:
        import fcntl
    except ImportError:
        return _append_csv(fn, head, rows, key)
    lkd = os.path.join(os.path.dirname(fn) or '.', '.locks'); os.makedirs(lkd, exist_ok=True)
    with open(os.path.join(lkd, os.path.basename(fn) + '.lock'), 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _append_csv(fn, head, rows, key)


def _append_csv(fn, head, rows, key=2):
    seen = set()
    if os.path.exists(fn):
        rd = list(csv.reader(open(fn, encoding='utf-8-sig')))
        if rd and rd[0] != head:
            os.replace(fn, fn[:-4] + '_v1.csv'); rd = []
        seen = {tuple(r[:key]) for r in rd[1:]}
    nz = lambda x: '' if x is None else str(x)
    new = [r for r in rows if r[1] and tuple(nz(x) for x in r[:key]) not in seen]
    with open(fn, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        if not seen and not (os.path.exists(fn) and os.path.getsize(fn) > 3): w.writerow(head)
        w.writerows(new)
    return len(new)


HIST = lambda *a: os.path.join(DATA, 'history', *a)
T0 = time.time()      # start of this run (for runs_<month>.csv)


def archive(snap):
    """keep every measured reading (for later models: machine learning, reinforcement learning, re-calibration)
       data/history/<kind>_<YYYY-MM>.csv  one row per station and measured time (local time), never overwritten
       data/history/snap/<YYYYMMDD_HH>.json.gz  the whole snapshot of this update"""
    import gzip
    os.makedirs(HIST('snap'), exist_ok=True)
    loc = lambda u: (datetime.fromisoformat(u) + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M') if u else None
    now = datetime.now(timezone(timedelta(hours=7))); mon = now.strftime('%Y-%m')
    with gzip.open(HIST('snap', now.strftime('%Y%m%d_%H') + '.json.gz'), 'wt', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, separators=(',', ':'))
    kinds = dict(
        canal=(['code', 'measured', 'wl_in_m', 'wl_out_m', 'wl_out2_m', 'gate1', 'gate2', 'gate3', 'status', 'max_in_today_m'],
               [(x[0], loc(x[11]), x[13], x[14], x[15], x[8], x[9], x[10], x[12], x[22]) for x in snap['W']]),
        road=(['code', 'measured', 'depth_cm', 'max_cm', 'status'], [(x[0], loc(x[8]), x[9], x[10], x[12]) for x in snap['F']]),
        rain=(['code', 'measured', 'rf15m_mm', 'rf1h_mm', 'rf3h_mm', 'rf6h_mm', 'rf12h_mm', 'rf24h_mm'],
              [(x[0], loc(x[6]), x[7], x[8], x[9], x[10], x[11], x[12]) for x in snap['R']]),
        tunnel=(['code', 'measured', 'depth_cm', 'max_cm'], [(x[0], loc(x[4]), x[5], x[6]) for x in snap['T']]))
    n = sum(append_csv(HIST(f'{k}_{mon}.csv'), h, r) for k, (h, r) in kinds.items())
    # station lists with coordinates, banks and thresholds (they change rarely; a new row only when something changes)
    st = (['code', 'name', 'district_id', 'district', 'lat', 'lon', 'system', 'gates', 'left_bank_m', 'right_bank_m',
           'warning_m', 'critical_m', 'warning_out_m', 'critical_out_m', 'river', 'first_seen'],
          [(x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[16], x[17], x[18], x[19], x[20], x[21], x[24], now.strftime('%Y-%m-%d %H:%M')) for x in snap['W']])
    append_csv(HIST('canal_stations.csv'), *st, key=15)
    append_csv(HIST('road_sensors.csv'), ['code', 'name', 'road', 'district_id', 'district', 'lat', 'lon', 'type', 'first_seen'],
               [(x[0], x[2], x[3], x[4], x[5], x[6], x[7], x[13], now.strftime('%Y-%m-%d %H:%M')) for x in snap['F']], key=8)
    append_csv(HIST('rain_stations.csv'), ['code', 'name', 'district_id', 'district', 'lat', 'lon', 'first_seen'],
               [(x[0], x[1], x[2], x[3], x[4], x[5], now.strftime('%Y-%m-%d %H:%M')) for x in snap['R']], key=6)
    print(f'history: +{n} readings (data/history)')


def backfill(max_gap_h=2.5):
    """after a gap (computer off or asleep): the BMA station pages keep the last ~48 h of hourly canal levels.
    If the previous update is more than `max_gap_h` hours ago, pull them and add the missing hours to the history
    (status 'backfill'). Road sensors and Traffy cannot be filled this way (Traffy is re-read 14 days back anyway)."""
    fn = HIST('last_update.txt'); now = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
    last = None
    if os.path.exists(fn):
        try: last = datetime.strptime(open(fn).read().strip(), '%Y-%m-%d %H:%M')
        except ValueError: last = None
    open(fn, 'w').write(now.strftime('%Y-%m-%d %H:%M'))
    if last is not None and (now - last).total_seconds() < max_gap_h * 3600: return
    if last is None:            # first run with the history: keep the hourly pull already on disk (1-3 Oct 2026)
        last = datetime(2000, 1, 1); print('history: adding the hourly canal levels already on disk (wr_raw.json)')
    else:
        print(f'gap since {last:%Y-%m-%d %H:%M} ({(now - last).total_seconds() / 3600:.0f} h): filling canal levels from the station pages')
        hourly()
    wr = json.load(open(P('wr_raw.json'), encoding='utf-8')); idmap = {str(a): b for a, b in json.load(open(P('idmap.json'), encoding='utf-8'))['w']}
    by = collections.defaultdict(list); t0 = last.strftime('%Y-%m-%d %H:%M')
    for wid, v in wr['water'].items():
        code = idmap.get(wid)
        if not code: continue
        for row in v.get('data') or []:
            try:
                d, t = (row[0].split(' ') + ['00:00'])[:2]; dd, mm, yy = d.split('/'); yy = int(yy) - (543 if int(yy) > 2400 else 0)
                ts = f'{yy:04d}-{int(mm):02d}-{int(dd):02d} {t}'
            except (ValueError, IndexError): continue
            if ts <= t0: continue
            val = lambda k: row[k] if len(row) > k and row[k] not in ('', '-') else None
            by[ts[:7]].append((code, ts, val(1), val(2), val(3), None, None, None, 'backfill', None))
    head = ['code', 'measured', 'wl_in_m', 'wl_out_m', 'wl_out2_m', 'gate1', 'gate2', 'gate3', 'status', 'max_in_today_m']
    n = sum(append_csv(HIST(f'canal_{m}.csv'), head, rows) for m, rows in by.items())
    print(f'backfill: +{n} hourly canal readings')


def export_daily(redo_days=3):
    """one CSV per kind and day (data/history/daily/<kind>/<kind>_YYYY-MM-DD.csv) for days that are over: these are the
    files pushed to GitHub (each is written once, so the repository grows only by the data itself). The last
    `redo_days` days are rewritten each time (backfill can still add to them)."""
    import glob
    today = datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d')
    redo = (datetime.now(timezone(timedelta(hours=7))) - timedelta(days=redo_days)).strftime('%Y-%m-%d')
    col = dict(canal=1, road=1, rain=1, tunnel=1, traffy=2, advice_pumps=0, advice_plan=0, runs=0, pump_hourly=1, river=1)
    n = 0
    for kind, c in col.items():
        days = collections.defaultdict(list); head = None
        for fn in sorted(glob.glob(HIST(f'{kind}_????-??.csv'))):
            rd = csv.reader(open(fn, encoding='utf-8-sig')); h = next(rd, None)
            if head is None: head = h
            elif h != head: continue
            for r in rd:
                if len(r) > c and r[c][:10] < today: days[r[c][:10]].append(r)
        if not head: continue
        os.makedirs(HIST('daily', kind), exist_ok=True)
        for d, rows in days.items():
            out = HIST('daily', kind, f'{kind}_{d}.csv')
            if os.path.exists(out) and d < redo: continue
            with open(out, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f); w.writerow(head); w.writerows(sorted(rows, key=lambda r: (r[c], r[0])))
            n += 1
    if n: print(f'history: {n} daily files written (data/history/daily)')


def archive_outputs():
    """after the models: what was measured and what the models advised at this update (state -> action log),
    the pump volumes, and the rain forecast that was used (to score forecasts against the rain that fell)"""
    import gzip
    now = datetime.now(timezone(timedelta(hours=7))); run = now.strftime('%Y-%m-%d %H:%M'); mon = now.strftime('%Y-%m')
    if os.path.exists(P('pump_daily.csv')):
        rows = list(csv.reader(open(P('pump_daily.csv'), encoding='utf-8-sig')))
        if rows: append_csv(HIST('pump_daily.csv'), rows[0], rows[1:])
    if os.path.exists(P('forecast_raw.json')):
        FR = json.load(open(P('forecast_raw.json'), encoding='utf-8'))
        os.makedirs(HIST('forecast'), exist_ok=True)
        fn = HIST('forecast', now.strftime('%Y%m%d_%H') + '.json.gz')
        if not os.path.exists(fn):
            with gzip.open(fn, 'wt', encoding='utf-8') as f:
                json.dump({k: FR[k] for k in ('fetched', 'source', 'time', 'points', 'det')}, f, separators=(',', ':'))
    if os.path.exists(P('forecast.json')):
        F = json.load(open(P('forecast.json'), encoding='utf-8'))
        rows = []
        for sc in F['scenarios']:
            for p in sc['pumps']:
                rows.append((run, sc['key'], p['id'], p['now'], p['series'][0], max(p['series']), p.get('start'), p['cap']))
        append_csv(HIST(f'advice_pumps_{mon}.csv'), ['run', 'scenario', 'pump', 'measured_m3s', 'advised_first_m3s', 'advised_max_m3s',
                                                     'start_step', 'capacity_m3s'], rows, key=3)
    if os.path.exists(P('plan.json')):
        PL = json.load(open(P('plan.json'), encoding='utf-8'))
        rows = [(run, sc, z, d.get('50%'), d.get('90%'), PL['volume0'].get(z), PL['assumptions']['lambda_zone'].get(z))
                for sc in ('current', 'plan', 'mobile', 'trunk') for z, d in PL[sc]['days'].items()]
        append_csv(HIST(f'advice_plan_{mon}.csv'), ['run', 'scenario', 'zone', 'days_50', 'days_90', 'volume0_mm3', 'lambda'], rows, key=3)
    archive_model(now)
    export_daily()


def code_version():
    """short fingerprint of the model code (scripts/*.py, *.html, *.js): changes whenever the model changes"""
    import glob, hashlib
    h = hashlib.sha1()
    for fn in sorted(glob.glob(os.path.join(HERE, '*.py')) + glob.glob(os.path.join(HERE, '*.html')) + glob.glob(os.path.join(HERE, '*.js'))):
        h.update(os.path.basename(fn).encode()); h.update(open(fn, 'rb').read().replace(b'\r\n', b'\n'))
    return h.hexdigest()[:10]


def archive_model(now, t_start=None):
    """what the models predicted at this run, to score them later against what was measured:
       data/history/model/<YYYYMMDD_HH>_forecast.json.gz  48-h canal levels per station, zones, pumps (forecast + extreme)
       data/history/model/<YYYYMMDD_HH>_plan.json.gz      30-day plan (and _plan_v068 sensitivity)
       data/history/model/<YYYYMMDD_HH>_flood.json.gz     flood map (every 3 h)
       data/history/runs_<YYYY-MM>.csv                    one row per run: code version, inputs, key parameters"""
    import gzip
    os.makedirs(HIST('model'), exist_ok=True); tag = now.strftime('%Y%m%d_%H')
    files = [('forecast.json', 'forecast'), ('plan.json', 'plan'), ('plan_v068.json', 'plan_v068')] + ([('flood.json', 'flood')] if now.hour % 3 == 0 else [])
    for src, name in files:
        out = HIST('model', f'{tag}_{name}.json.gz')
        if os.path.exists(P(src)) and not os.path.exists(out):
            with open(P(src), 'rb') as f, gzip.open(out, 'wb') as g: g.write(f.read())
    S = json.load(open(P('snapshot.json'), encoding='utf-8')) if os.path.exists(P('snapshot.json')) else {}
    PL = json.load(open(P('plan.json'), encoding='utf-8')) if os.path.exists(P('plan.json')) else {}
    FC = json.load(open(P('forecast.json'), encoding='utf-8')) if os.path.exists(P('forecast.json')) else {}
    A = PL.get('assumptions', {}); lam = A.get('lambda_zone') or {}
    av = A.get('avail'); av = av if isinstance(av, (int, float)) else None
    ok = lambda k, i: sum(1 for x in S.get(k, []) if x[i] not in (None, '', -99))
    tot = {sc['key']: sc.get('total_mm') for sc in FC.get('scenarios', [])}
    import socket
    row = (now.strftime('%Y-%m-%d %H:%M'), code_version(), socket.gethostname(), S.get('fetched'),
           ok('W', 13), len(S.get('W', [])), ok('F', 9), len(S.get('F', [])), ok('R', 8), len(S.get('R', [])),
           FC.get('forecast_fetched'), tot.get('forecast'), tot.get('extreme'), A.get('v'), av,
           *[lam.get(z) for z in ('ชั้นในและฝั่งตะวันตก', 'เหนือ', 'ตะวันออกรอบนอก')],
           round(time.time() - (t_start or T0)))
    append_csv(HIST(f'runs_{now:%Y-%m}.csv'),
               ['run', 'code_version', 'host', 'bma_fetched_utc', 'canal_ok', 'canal_n', 'road_ok', 'road_n', 'rain_ok', 'rain_n',
                'rain_forecast_fetched', 'forecast_total_mm', 'extreme_total_mm', 'plan_v', 'pump_avail_default',
                'lambda_inner_west', 'lambda_north', 'lambda_east', 'run_seconds'], [row], key=1)


def snapshot():
    t0 = time.time()
    W = json.loads(get('/water/PageMap/GoogleMap', b''))
    F = json.loads(get('/Flood/PageMap/GetData?id=0'))
    R = json.loads(get('/rain/PageMap/GetDataForUpdate', b''))
    snap = dict(fetched=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'), W=[], R=[], F=[], T=[])
    for w in W:
        snap['W'].append([w['water_code'], w.get('water_shortname') or w['water_name'], w['district_id'], w['district_name'], w['latitude'], w['longitude'],
                          w['water_system_id'], w['water_gate_count'], w['watergate01'], w['watergate02'], w['watergate03'],
                          ms_to_utc(w['site_timestamp']), w['txtStatus_en'], w['wl_in'], w['wl_out01'], w['wl_out02'],
                          w['left_bank'], w['right_bank'], w['warning'], w['critical'], w['warning_out01'], w['critical_out01'],
                          w['max_in_day'], w['max_in_yesterday'], w['river_name']])
    # road sensors: since 5 Oct 2026 dtTbl has no district_id / coordinates / epoch time; floodTbl carries the coordinates.
    # Merge both by code and fall back between the old and new field names, so either format works.
    did_of = {}
    for w in W + R:
        if w.get('district_name') and w.get('district_id') is not None: did_of[w['district_name']] = w['district_id']
    if os.path.exists(P('road_sensor.csv')):
        for r in csv.DictReader(open(P('road_sensor.csv'), encoding='utf-8-sig')):
            if r.get('district') and r.get('district_id'): did_of.setdefault(r['district'], int(r['district_id']))
    extra = {f.get('flood_code'): f for f in (F.get('floodTbl') or [])}
    for f0 in F['dtTbl']:
        f = {**extra.get(f0.get('flood_code'), {}), **{k: v for k, v in f0.items() if v is not None}}
        dname = f.get('districtName') or f.get('district_name')
        ts = ms_to_utc(f.get('site_timestamp') or f.get('site_timestatmpTH'))
        snap['F'].append([f['flood_code'], f.get('flood_id'), f.get('flood_shortname') or f.get('flood_short_name') or f.get('flood_name'),
                          f.get('road_name'), f.get('district_id', did_of.get(dname)), dname, f.get('latitude'), f.get('longitude'), ts,
                          f.get('flood'), f.get('flood_max'), f.get('flood_max_time'), f.get('chkStatustxt_en') or 'Normal', f.get('typesite')])
    for t in F['dtTblTunel']:
        snap['T'].append([t['flood_code'], t.get('flood_name'), t.get('latitude'), t.get('longitude'), ms_to_utc(t.get('site_timestamp') or t.get('site_timestatmpTH')), t.get('flood'), t.get('flood_max')])
    for r in R:
        snap['R'].append([r['rain_code'], r['rain_shortname'], r['district_id'], r['district_name'], r['latitude'], r['longitude'],
                          ms_to_utc(r['site_timestamp']), r['rf15min'], r['rf1hr'], r['rf3hr'], r['rf6hr'], r['rf12hr'], r['rf24hr'],
                          r['rf_7_now'], r['rf_0_now']])
    json.dump(snap, open(P('snapshot.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    try: archive(snap)
    except Exception as e: print('บันทึกประวัติไม่สำเร็จ (ข้าม):', e)
    # keep the water_id -> code map current (used for the hourly pull)
    json.dump(dict(w=[[w['water_id'], w['water_code']] for w in W]), open(P('idmap.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'snapshot: canal {len(W)} · road {len(snap["F"])} · rain {len(R)} · tunnel {len(snap["T"])} ({time.time() - t0:.0f} s)')


def table_rows(page):
    m = re.search(r'<table[^>]*id="example".*?</table>', page, re.S)
    if not m: return []
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', m[0], re.S):
        cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if cells: rows.append(cells)
    return rows


def pumps(max_days=14):
    raw = json.load(open(P('pump_raw.json'), encoding='utf-8')) if os.path.exists(P('pump_raw.json')) else []
    have = {r[0] for r in raw}
    today = datetime.now(timezone(timedelta(hours=7))).date()
    todo = [today - timedelta(days=k) for k in range(1, max_days + 1)]
    todo = [d for d in todo if d.strftime('%d/%m/%Y') not in have][::-1]
    for d in todo:
        s = d.strftime('%d/%m/%Y')
        rows = table_rows(get('/water/WaterPumpHistory', dict(waterIdField='A1', dateStart=s, dateEnd=s, TimeStart='00:00', timeEnd='23:59'), timeout=90))
        rows = [r for r in rows if len(r) == 5 and r[1].startswith('WL.')]
        raw += [[s] + r for r in rows]
        print(f'pump volumes {s}: {len(rows)} stations'); time.sleep(1)
    json.dump(raw, open(P('pump_raw.json'), 'w', encoding='utf-8'), ensure_ascii=False)


def hourly(workers=4):
    idmap = json.load(open(P('idmap.json'), encoding='utf-8'))['w']
    wr = json.load(open(P('wr_raw.json'), encoding='utf-8'))

    def one(item):
        wid, code = item
        try:
            page = get(f'/water/StationDetail?id={wid}', timeout=90)
        except Exception as e:
            return wid, None
        head = re.findall(r'<th[^>]*>(.*?)</th>', re.search(r'<table[^>]*id="example".*?</thead>', page, re.S)[0], re.S) if 'id="example"' in page else []
        rows = [r[1:] for r in table_rows(page) if len(r) > 2 and r[1].endswith(':00')]   # keep on-the-hour readings
        name = re.search(r'<title>(.*?)</title>', page, re.S)
        return wid, dict(name=html.unescape(name[1]).strip() if name else code, head=[re.sub(r'<[^>]+>', '', h).strip() for h in head], data=rows)
    t0 = time.time(); ok = 0
    with ThreadPoolExecutor(workers) as ex:
        for k, (wid, v) in enumerate(ex.map(one, idmap)):
            if v and v['data']: wr['water'][str(wid)] = v; ok += 1
            if k % 25 == 0: print(f'  hourly {k}/{len(idmap)}')
    json.dump(wr, open(P('wr_raw.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'hourly levels: {ok} stations ({time.time() - t0:.0f} s)')


def traffy(days=14):
    """flood reports from Traffy Fondue (public API), last `days` days -> data/traffy_flood.json"""
    end = datetime.now(timezone(timedelta(hours=7))).date(); start = end - timedelta(days=days)
    SC = dict(start=0, inprogress=1, forward=2, follow=3, finish=4, irrelevant=5)
    rows = []; arch = []; off = 0
    while off < 60000:
        q = urllib.parse.urlencode(dict(output_format='geojson', start=str(start), end=str(end + timedelta(days=1)),
                                        problem_type='น้ำท่วม', limit=1000, offset=off))
        req = urllib.request.Request('https://publicapi.traffy.in.th/teamchadchart-stat-api/geojson/v1?' + q, headers=UA)
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            j = json.loads(r.read().decode('utf-8'))
        fs = j.get('features') or []
        for f in fs:
            p = f['properties']; lon, lat = f['geometry']['coordinates']
            recent = p.get('timestamp', '') >= str(end - timedelta(days=4)) and SC.get(p.get('state_type_latest'), 9) < 4
            arch.append((p.get('ticket_id'), p.get('state_type_latest') or '', (p.get('timestamp') or '')[:16].replace('T', ' '),
                         (p.get('last_activity') or '')[:16].replace('T', ' '), round(lon, 6), round(lat, 6), p.get('district') or '',
                         p.get('subdistrict') or '', re.sub(r'\s+', ' ', p.get('description') or ''), p.get('photo_url') or ''))
            rows.append([p.get('ticket_id'), round(lon, 5), round(lat, 5), (p.get('timestamp') or '')[5:16], SC.get(p.get('state_type_latest'), 9),
                         p.get('district') or '', re.sub(r'\s+', ' ', p.get('description') or '')[:60] if recent else ''])
        if len(fs) < 1000: break
        off += 1000; time.sleep(1)
    out = dict(fetched=datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M'),
               source=f'Traffy Fondue (publicapi.traffy.in.th) ประเภท น้ำท่วม {start} ถึง {end}',
               cols=['ticket', 'lon', 'lat', 'time', 'state', 'district', 'desc'],
               states=['รอรับเรื่อง', 'กำลังดำเนินการ', 'ส่งต่อ', 'ติดตาม', 'เสร็จสิ้น', 'ไม่เกี่ยวข้อง'], rows=rows)
    json.dump(out, open(P('traffy_flood.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    os.makedirs(HIST(), exist_ok=True)
    by = collections.defaultdict(list)
    for r in arch: by[r[2][:7] or 'unknown'].append(r)
    nn = sum(append_csv(HIST(f'traffy_{k}.csv'), ['ticket', 'state', 'reported', 'last_activity', 'lon', 'lat', 'district',
                                                   'subdistrict', 'description', 'photo_url'], v) for k, v in by.items())
    print(f'Traffy flood reports: {len(rows)} (+{nn} new or changed in data/history)')


def forecast():
    """48-72 h hourly rain forecast on a 5x5 grid over Bangkok from Open-Meteo (free, no key) -> data/forecast_raw.json"""
    la, lo = [], []
    for a in (13.55, 13.65, 13.75, 13.85, 13.93):
        for b in (100.36, 100.48, 100.60, 100.72, 100.84): la.append(a); lo.append(b)
    q = f"latitude={','.join(map(str, la))}&longitude={','.join(map(str, lo))}&hourly=precipitation&forecast_days=5&timezone=Asia%2FBangkok"
    def js(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90, context=CTX) as r: return json.loads(r.read().decode('utf-8'))
    det = js('https://api.open-meteo.com/v1/forecast?' + q)
    ens = js('https://ensemble-api.open-meteo.com/v1/ensemble?' + q + '&models=ecmwf_ifs025')
    r1 = lambda v: 0 if v is None else round(v, 1)
    out = dict(fetched=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
               source='Open-Meteo forecast (best_match) + ensemble ECMWF IFS 0.25 (51 members)', time=det[0]['hourly']['time'],
               points=[[d['latitude'], d['longitude']] for d in det], det=[[r1(v) for v in d['hourly']['precipitation']] for d in det],
               ens=[[[r1(v) for v in d['hourly'][k]] for k in d['hourly'] if k.startswith('precipitation')] for d in ens])
    json.dump(out, open(P('forecast_raw.json'), 'w'), separators=(',', ':'))
    print(f"rain forecast: {len(det)} points, {len(out['ens'][0])} ensemble members")


def rebuild(plan=True):
    """build.py (tables/CSV) -> build_model.py -> [plan_lp.py x2 -> build_model.py -> forecast_lp.py] -> build_flood.py -> render.py"""
    env = dict(os.environ, PYTHONUTF8='1')
    run = lambda s, extra=None: subprocess.run([sys.executable, os.path.join(HERE, s)], check=True, env=dict(env, **(extra or {})))
    run('build.py'); run('build_model.py')
    if plan:   # multi-period LP needs numpy/scipy/networkx; skipped (old plan kept) if they are not installed
        try:
            import numpy, scipy, networkx  # noqa: F401
            print('คำนวณแผนระบาย 30 วัน (LP หลายช่วงเวลา) ราว 1–3 นาที...')
            run('plan_lp.py')
            run('plan_lp.py', dict(PLAN_V='0.68', PLAN_OUT='plan_v068.json', PLAN_LAMBDA_FROM='plan.json'))   # sensitivity: faster canal flow
            run('build_model.py')
            import shapely, sklearn  # noqa: F401
            if os.path.exists(P('forecast_raw.json')):
                print('คำนวณแผนพร่องน้ำตามฝนคาดการณ์ 48 ชม. ราว 2 นาที...'); run('forecast_lp.py')
            if os.path.exists(P('osm_outer.json')): run('build_outer.py')      # where boundary outlets drain outside BKK
        except ImportError:
            print('ข้ามแผนระบาย: ยังไม่ได้ติดตั้ง numpy scipy networkx (pip install numpy scipy networkx)')
    if not os.path.exists(P('road_segments.json')):   # once: road geometry under each flood sensor (OpenStreetMap)
        try: run('build_roads.py')
        except Exception as e: print('ข้ามเส้นถนน (ต้องต่ออินเทอร์เน็ตครั้งแรก):', e)
    if not os.path.exists(P('road_net.json')):         # once: main roads for the flood map (OpenStreetMap)
        try: run('build_roadnet.py')
        except Exception as e: print('ข้ามถนนสายหลัก (ต้องต่ออินเทอร์เน็ตครั้งแรก):', e)
    if os.path.exists(P('forecast.json')) and os.path.exists(P('forecast_raw.json')):   # flood map (depth, roads) for the forecast page
        try: run('build_flood.py')
        except Exception as e: print('ข้ามแผนที่น้ำท่วม:', e)
    run('render.py')


if __name__ == '__main__':
    args = set(sys.argv[1:])
    if '--offline' in args:          # rebuild from the data already on disk (no download)
        rebuild(plan='--no-plan' not in args); sys.exit(0)
    try:
        snapshot()
        try: backfill()
        except Exception as e: print('เติมช่วงที่ขาดไม่สำเร็จ (ข้าม):', e)
        if '--no-pump' not in args: pumps()
        if '--hourly' in args: hourly()
        if '--no-forecast' not in args:
            try: forecast()
            except Exception as e: print('ดึงฝนคาดการณ์ไม่สำเร็จ (ใช้ชุดเดิม):', e)
        if '--no-traffy' not in args:
            try: traffy()
            except Exception as e: print('ดึงรายงาน Traffy ไม่สำเร็จ (ใช้ชุดเดิม):', e)
    except Exception as e:
        print('\nดึงข้อมูลไม่สำเร็จ:', e)
        print('ตรวจว่าเครื่องต่ออินเทอร์เน็ต และเปิด https://weather.bangkok.go.th/water/PageMap ในเบราว์เซอร์ได้ แล้วลองใหม่')
        print('(หน้าเว็บยังใช้ข้อมูลชุดเดิมได้ตามปกติ)')
        sys.exit(1)
    rebuild(plan='--no-plan' not in args)
    try: archive_outputs()
    except Exception as e: print('บันทึกผลโมเดลไม่สำเร็จ (ข้าม):', e)
    print('\nเสร็จแล้ว: เปิดหน้าเว็บใหม่ (F5)')
