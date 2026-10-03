"""Calibrate the canal network with published facts (run once after build_network.py; safe to re-run).

1. Canal widths: BMA flood plan 2557, annex A (canal maintenance list: width range and length per canal)
   -> data/canal_widths2557.json. For every canal named there, the width used for capacity and storage is the
   length-weighted mid width instead of the class default (A 20 m, B 10 m, C 5 m). Depth stays the class default.
2. Bueng Nong Bon tunnel (same report: 5 m diameter, 9.4 km, 60 m3/s pump at the outlet = "อาคารสูบน้ำพระโขนง"):
   added as a link from the canal next to Bueng Nong Bon to the Phra Khanong pump node, capacity 60 m3/s.
3. Canal surface around each node (storage) recomputed from the widths.

Edits data/network.json in place; the original OSM-class values are kept in cap_osm / w_osm.
"""
import json, os, re, math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(HERE, '..', 'data')
P = lambda f: os.path.join(DATA, f)
net = json.load(open(P('network.json'), encoding='utf-8'))
W = json.load(open(P('canal_widths2557.json'), encoding='utf-8'))['canals']
TONE = re.compile('[่-์]')
key = lambda s: TONE.sub('', (s or '').replace(' ', ''))
wkey = {key(k): v for k, v in W.items()}
CL = net['classes']; V0 = net['V0']; E = net['edges']; N = net['nodes']; names = net['names']

# drop an earlier tunnel so the script can be re-run
TUN = 'อุโมงค์ระบายน้ำบึงหนองบอน'
E[:] = [e for e in E if not e.get('tunnel')]
hit = set()
for e in E:
    if 'cap_osm' not in e: e['cap_osm'] = e['cap0']
    cw, cd = CL[e['c']]['w'], CL[e['c']]['d']
    base = V0 * cw * cd
    feeder = e['cap_osm'] > base + 1e-6                 # raised because it feeds a pump (build_network)
    nm = names[e['n']] if e['n'] is not None and e['n'] < len(names) else ''
    w = wkey.get(key(nm), {}).get('w_mid')
    e['w'] = w if w else cw
    if w: hit.add(nm)
    cap = V0 * e['w'] * cd
    e['cap0'] = round(max(cap, e['cap_osm']) if feeder else cap, 2)

# tunnel: Bueng Nong Bon -> Phra Khanong pump node
pk = next(p for p in net['pumps'] if 'อาคารสูบน้ำพระโขนง' in p['name'])
NB = (100.6600, 13.6940)                                # Bueng Nong Bon (Prawet): largest water body in the DEM mask there
KX = math.cos(math.radians(13.75)) * 111320; KY = 110574
d = [math.hypot((n['lon'] - NB[0]) * KX, (n['lat'] - NB[1]) * KY) for n in N]
# intake on a main canal next to the lake (the lake takes water from คลองประเวศบุรีรมย์ / คลองหนองบอน)
main = {x for e in E if names[e['n']] in ('คลองประเวศบุรีรมย์', 'คลองหนองบอน') for x in (e['u'], e['v'])}
nb = min(main, key=lambda i: d[i])
if TUN not in names: names.append(TUN)
a, b = N[nb], N[pk['node']]
L = 9400
E.append(dict(u=nb, v=pk['node'], n=names.index(TUN), c='A', L=L, cap0=60.0, cap_osm=60.0, w=0, st=None, tunnel=True,
              g=[[a['lon'], a['lat']], [b['lon'], b['lat']]]))

# storage: canal surface around each node = sum of half edge length x width
S = [0.0] * len(N)
for e in E:
    if e.get('tunnel'): continue
    S[e['u']] += e['L'] / 2 * e['w']; S[e['v']] += e['L'] / 2 * e['w']
for n, s in zip(N, S):
    if 'S_osm' not in n: n['S_osm'] = n['S']
    n['S'] = round(s)
net['adjust'] = dict(source='แผนปฏิบัติการฯ 2557: ความกว้างคลอง (ภาคผนวก ก) และอุโมงค์บึงหนองบอน 60 ลบ.ม./วิ.',
                     canals_matched=len(hit), tunnel=dict(name=TUN, from_node=nb, to_node=pk['node'], cap=60, km=9.4,
                                                          intake_dist_m=round(d[nb])))
json.dump(net, open(P('network.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('canals matched', len(hit), 'of', len(W), '· tunnel intake node', nb, round(d[nb]), 'm from Bueng Nong Bon')
for nm in ('คลองพระโขนง', 'คลองประเวศบุรีรมย์', 'คลองแสนแสบ', 'คลองหนองบอน', 'คลองลาดพร้าว'):
    caps = [e['cap0'] for e in E if names[e['n']] == nm]
    print(nm, 'cap', min(caps) if caps else None, '-', max(caps) if caps else None)
