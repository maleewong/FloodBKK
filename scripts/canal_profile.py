"""Water level along a canal line, from the gauges on it (measured data only), and the canal capacity it implies.

For each line (a chain of gauges along connected canals, upstream first):
  * chainage of every gauge along the canal (projected on the OSM canal network)
  * level now and over the last 48 h (data/canal_snapshot.csv = one row per update, canal_hourly.csv = 48-h pull)
  * per reach between two gauges: the drop in level per km (water-surface slope, median of the last 48 h)
    a reach with a much steeper slope than the rest of the line is a bottleneck (same flow, more head lost)
  * flow the canal carries now, from Manning's formula on the ordinary reaches:
        Q = (1/n) A R^(2/3) S^(1/2),   A = width x depth,  R = A / (width + 2 depth)
    width from the BMA 2557 canal report, depth and roughness n are assumptions (shown on the page).
    The upstream gauges are above their critical level, so this is the most the canal moves with the head it has:
    build_model.py uses it as the capacity of the canal in the drainage model (instead of width x depth x 0.5 m/s).
Stdlib only. Used by build_model.py.
"""
import csv, heapq, json, math, os, statistics
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574

LINES = [dict(
    key='prawet',
    title='คลองประเวศบุรีรมย์ → คลองพระโขนง → สถานีสูบน้ำพระโขนง',
    canals=['คลองประเวศบุรีรมย์', 'คลองพระโขนง'],
    gauges=['WL.PWT.07', 'WL.PWT.06', 'WL.PWT.05', 'WL.PWT.04', 'WL.PWT.03', 'WL.PWT.02', 'WL.PWT.01', 'WL.PKN.01'],
    pump='WL.PKN.01',                 # last gauge = pump sump: its reach is drawn down by the pumps, not used for the flow
    calibrate='คลองประเวศบุรีรมย์',  # model capacity of this canal is set from the measured flow
    width=33.3, depth=2.5, n=0.030,   # width: BMA 2557 report (median, weighted by length); depth, n: assumptions
), dict(
    key='saensaep',
    title='คลองแสนแสบ: หนองจอก → มีนบุรี → บางกะปิ → ประตูน้ำ',
    canals=['คลองแสนแสบ'],
    gauges=['WL.SSB.13', 'WL.SSB.12', 'WL.SSB.11', 'WL.SSB.10', 'WL.SSB.09', 'WL.SSB.08', 'WL.SSB.07', 'WL.SSB.06',
            'WL.SSB.04', 'WL.SSB.03', 'WL.SSB.02', 'WL.SSB.01'],
    gates=['WL.SSB.12', 'WL.SSB.10', 'WL.SSB.09', 'WL.SSB.04'],   # gate / pump structures: head lost there is not friction
    pump=None, calibrate=None,        # shown only: the gates control the levels, so a single canal flow is not derived
    width=27.3, depth=3.0, n=0.030,
)]
STEEP = 2.0      # a reach is a bottleneck when its slope is >= STEEP x the median slope of the line
MAX_OFF_M = 300  # gauges farther than this from the mapped canal are shown but not used (position along the canal unsure)
WINDOW_H = 48


def _num(x):
    try:
        v = float(x)
        return None if v <= -90 else v
    except (TypeError, ValueError):
        return None


def chainage(line, net, stations):
    """distance (km) of each gauge along the line's canals, from the first gauge"""
    names = net['names']; nodes = net['nodes']
    nm = lambda e: names[e['n']] if e['n'] is not None else ''
    edges = [e for e in net['edges'] if nm(e) in line['canals'] and e.get('g')]
    adj = {}
    for e in edges:
        adj.setdefault(e['u'], []).append((e['v'], e['L'])); adj.setdefault(e['v'], []).append((e['u'], e['L']))

    def project(lon, lat):   # nearest point on any edge: (edge, metres from its u end, distance off)
        best = None
        for e in edges:
            g = e['g']; acc = 0.0; tot = sum(math.hypot((b[0] - a[0]) * KX, (b[1] - a[1]) * KY) for a, b in zip(g, g[1:])) or 1
            for a, b in zip(g, g[1:]):
                ax, ay = (a[0] - lon) * KX, (a[1] - lat) * KY; bx, by = (b[0] - lon) * KX, (b[1] - lat) * KY
                dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy or 1
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / L2)); d = math.hypot(ax + t * dx, ay + t * dy)
                seg = math.sqrt(L2)
                if best is None or d < best[2]: best = (e, (acc + t * seg) / tot * e['L'], d)
                acc += seg
        return best

    g0 = stations[line['gauges'][0]]; e0, s0, _ = project(g0['lon'], g0['lat'])
    dist = {e0['u']: s0, e0['v']: e0['L'] - s0}; pq = [(d, n) for n, d in dist.items()]; heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18): continue
        for v, L in adj.get(u, []):
            if d + L < dist.get(v, 1e18): dist[v] = d + L; heapq.heappush(pq, (d + L, v))
    out = {}
    for code in line['gauges']:
        s = stations.get(code)
        if not s: continue
        e, se, off = project(s['lon'], s['lat'])
        if e is e0: km = abs(se - s0)
        else: km = min(dist.get(e['u'], 1e18) + se, dist.get(e['v'], 1e18) + e['L'] - se)
        out[code] = (round(km / 1000, 2), round(off))
    return out


def history(codes, latest):
    """{code: {hour 'YYYY-MM-DD HH': level}} over the last WINDOW_H hours (snapshots of each update + the 48-h pull)"""
    h = {c: {} for c in codes}; t0 = (datetime.fromisoformat(latest) - timedelta(hours=WINDOW_H)).strftime('%Y-%m-%d %H')
    for f, tcol, vcol in (('canal_hourly.csv', 'ts', 'wl_in_m'), ('canal_snapshot.csv', 'measured', 'wl_in_m')):
        if not os.path.exists(P(f)): continue
        for r in csv.DictReader(open(P(f), encoding='utf-8-sig')):
            c = r['code']
            if c not in h or not r.get(tcol): continue
            hk = r[tcol][:13]; v = _num(r.get(vcol))
            if v is not None and hk >= t0: h[c][hk] = v
            if f == 'canal_snapshot.csv' and r.get('status') == 'ขัดข้อง': h[c].pop(hk, None)
    return h


def manning(w, d, n, s):
    A = w * d; R = A / (w + 2 * d)
    return (1 / n) * A * R ** (2 / 3) * math.sqrt(max(s, 0))


def compute(line, net, stations, latest):
    ch = chainage(line, net, stations)
    hist = history(line['gauges'], latest)
    gauges = []
    for code in line['gauges']:
        s = stations.get(code)
        if not s or code not in ch: continue
        hh = sorted(hist[code].items())
        vals = [v for _, v in hh]
        gauges.append(dict(code=code, name=s['name'], district=s.get('district'), km=ch[code][0], off_m=ch[code][1],
                           now=s['wl_in'] if s.get('status') != 'ขัดข้อง' else None, status=s.get('status'), measured=s.get('measured'),
                           warn=s.get('warning'), crit=s.get('critical'), bank=min([b for b in (s.get('left_bank'), s.get('right_bank')) if b is not None], default=None),
                           lo48=min(vals) if vals else None, hi48=max(vals) if vals else None, n48=len(vals)))
    gauges.sort(key=lambda g: g['km'])
    for g in gauges: g['used'] = g['now'] is not None and g['off_m'] <= MAX_OFF_M
    ok = [g for g in gauges if g['used']]
    reaches = []
    for a, b in zip(ok, ok[1:]):
        L = b['km'] - a['km']
        if L < 0.3: continue
        hrs = sorted(set(hist[a['code']]) & set(hist[b['code']]))
        s48 = [(hist[a['code']][k] - hist[b['code']][k]) / L for k in hrs]
        s_now = (a['now'] - b['now']) / L
        reaches.append(dict(up=a['code'], down=b['code'], km0=a['km'], km1=b['km'], L=round(L, 2),
                            drop=round(a['now'] - b['now'], 2), slope=round(s_now, 4),
                            slope48=round(statistics.median(s48), 4) if len(s48) >= 6 else round(s_now, 4), n48=len(s48),
                            pump=(b['code'] == line.get('pump')),
                            gate=(a['code'] in line.get('gates', ()) or b['code'] in line.get('gates', ()))))
    use = [r for r in reaches if not r['pump'] and not r['gate'] and r['slope48'] > 0]
    med = statistics.median([r['slope48'] for r in use]) if use else None
    for r in reaches:
        r['ratio'] = round(r['slope48'] / med, 2) if med else None
        r['bottleneck'] = bool(med and not r['pump'] and not r['gate'] and r['slope48'] >= STEEP * med)
        r['conveyance'] = round(math.sqrt(med / r['slope48']), 2) if med and r['slope48'] > 0 else None
    ref = [r for r in use if not r['bottleneck']]
    qs = [manning(line['width'], line['depth'], line['n'], r['slope48'] / 1000) for r in ref]
    q = round(statistics.median(qs), 1) if qs else None
    lo = round(manning(line['width'], line['depth'], 0.035, statistics.median([r['slope48'] for r in ref]) / 1000), 1) if ref else None
    hi = round(manning(line['width'], line['depth'], 0.025, statistics.median([r['slope48'] for r in ref]) / 1000), 1) if ref else None
    return dict(key=line['key'], title=line['title'], canal=line.get('calibrate'), gates=line.get('gates', []), gauges=gauges, reaches=reaches,
                median_slope=round(med, 4) if med else None, q=q, q_range=[lo, hi],
                assume=dict(width=line['width'], depth=line['depth'], n=line['n']), latest=latest,
                window_h=WINDOW_H)


def compute_all(net, snap_st, latest):
    """snap_st: {code: dict(name, lon, lat, wl_in, status, warning, critical, left_bank, right_bank, district, measured)}"""
    out = []
    for line in LINES:
        try:
            out.append(compute(line, net, snap_st, latest))
        except Exception as e:   # never stop the update for this
            print('canal profile', line['key'], 'skipped:', e)
    return out
