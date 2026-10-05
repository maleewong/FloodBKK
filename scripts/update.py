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
import csv, json, os, re, sys, time, subprocess, ssl, html
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
    rows = []; off = 0
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
            rows.append([p.get('ticket_id'), round(lon, 5), round(lat, 5), (p.get('timestamp') or '')[5:16], SC.get(p.get('state_type_latest'), 9),
                         p.get('district') or '', re.sub(r'\s+', ' ', p.get('description') or '')[:60] if recent else ''])
        if len(fs) < 1000: break
        off += 1000; time.sleep(1)
    out = dict(fetched=datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M'),
               source=f'Traffy Fondue (publicapi.traffy.in.th) ประเภท น้ำท่วม {start} ถึง {end}',
               cols=['ticket', 'lon', 'lat', 'time', 'state', 'district', 'desc'],
               states=['รอรับเรื่อง', 'กำลังดำเนินการ', 'ส่งต่อ', 'ติดตาม', 'เสร็จสิ้น', 'ไม่เกี่ยวข้อง'], rows=rows)
    json.dump(out, open(P('traffy_flood.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f'Traffy flood reports: {len(rows)}')


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
    print('\nเสร็จแล้ว: เปิดหน้าเว็บใหม่ (F5)')
