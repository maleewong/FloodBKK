"""BMA drainage tunnels in operation, added to the canal network as links from the intake canal to the outlet pump.

Sources (checked 4 Oct 2026):
  sizes, capacity, districts: กรุงเทพธุรกิจ 22 ก.ค. 2569 (https://www.bangkokbiznews.com/social/963855),
                              ThaiPublica 2566 (https://thaipublica.org/2023/03/thaipublica-survey-mission-to-solve-floods-in-bangkok/)
  intake / outlet:            JS100 "รู้จัก 5 อุโมงค์ระบายน้ำ" (https://www.js100.com/en/site/news/view/164777),
                              InfoQuest (https://www.infoquest.co.th/?p=41162), ผู้จัดการ 2554 (Phra Khanong: tunnel pumps 60 + station)
A tunnel is a pipe: it moves water from its intake canal to its outlet pump without storing any (no canal surface).
Its outlet needs a pump: an existing station where the tunnel ends there (cap kept), otherwise one is added (src='tunnel').
Used by build_model.py (network.json itself is not changed). Replaces the single tunnel that adjust_network.py added.
Stdlib only.
"""
import math

KX = math.cos(math.radians(13.75)) * 111320; KY = 110574

# intake: point near the intake + canal name(s) to snap to; outlet: an existing pump id, or a new pump at a point
TUNNELS = [
    dict(name='อุโมงค์ระบายน้ำบึงหนองบอน', cap=60, km=9.4,
         intake=(100.6600, 13.6940, ['คลองประเวศบุรีรมย์', 'คลองหนองบอน']),
         outlet=dict(new='TN.NBN', name='อาคารสูบน้ำอุโมงค์บึงหนองบอน (ใกล้โรงกลั่นบางจาก)', at=(100.5915, 13.6891), cap=60),
         area='ประเวศ สวนหลวง พระโขนง บางนา'),
    dict(name='อุโมงค์ระบายน้ำคลองแสนแสบ-คลองลาดพร้าว (พระราม 9)', cap=60, km=5.11,
         intake=(100.6030, 13.7480, ['คลองลาดพร้าว']),
         outlet=dict(pump='RE44'),           # อาคารสูบน้ำพระโขนง 60 ลบ.ม./วิ. = the tunnel's 4 pumps (ผู้จัดการ 2554)
         area='ห้วยขวาง บางกะปิ บึงกุ่ม วัฒนา วังทองหลาง ลาดพร้าว'),
    dict(name='อุโมงค์ระบายน้ำบึงมักกะสัน', cap=45, km=5.98,
         intake=(100.5545, 13.7515, ['คลองสามเสน', 'มักกะสัน']),
         outlet=dict(pump='RE32'),           # สถานีสูบน้ำคลองขุดวัดช่องลม 45 ลบ.ม./วิ. (ยานนาวา) = the tunnel outlet
         area='วัฒนา ปทุมวัน ราชเทวี พญาไท ห้วยขวาง ดินแดง'),
    dict(name='อุโมงค์ระบายน้ำคลองบางซื่อ', cap=60, km=6.4,
         intake=(100.5760, 13.8010, ['คลองบางซื่อ']),
         outlet=dict(pump='WL.BSU.01'),      # ส.คลองบางซื่อ (เกียกกาย) measured station, its capacity kept
         area='ห้วยขวาง ดินแดง พญาไท จตุจักร ลาดพร้าว วังทองหลาง บางซื่อ ดุสิต'),
    dict(name='อุโมงค์ระบายน้ำคลองเปรมประชากร', cap=30, km=1.88,
         intake=(100.5360, 13.8060, ['คลองเปรมประชากร']),
         outlet=dict(new='TN.PRM', name='อาคารสูบน้ำอุโมงค์คลองเปรมประชากร (บางโพ)', at=(100.5175, 13.8081), cap=30),
         area='บางซื่อ จตุจักร หลักสี่ ดอนเมือง'),
]


def _d(n, lon, lat):
    return math.hypot((n['lon'] - lon) * KX, (n['lat'] - lat) * KY)


def apply(net):
    """add the tunnels to net (in memory); returns a list describing them for the page"""
    N, E, names, PU = net['nodes'], net['edges'], net['names'], net['pumps']
    E[:] = [e for e in E if not e.get('tunnel')]
    PU[:] = [p for p in PU if p.get('src') != 'tunnel']
    pid = {p['id']: p for p in PU}
    out = []
    for t in TUNNELS:
        lon, lat, canals = t['intake']
        cand = {x for e in E if e['n'] is not None and any(c in names[e['n']] for c in canals) for x in (e['u'], e['v'])}
        if not cand:
            print('tunnel skipped (intake canal not in network):', t['name']); continue
        a = min(cand, key=lambda i: _d(N[i], lon, lat))
        o = t['outlet']
        if 'pump' in o:
            p = pid.get(o['pump'])
            if not p:
                print('tunnel skipped (outlet pump missing):', t['name']); continue
        else:
            node = min(range(len(N)), key=lambda i: _d(N[i], *o['at']))
            p = dict(id=o['new'], name=o['name'], node=node, cap=float(o['cap']), src='tunnel', obs_max=None, internal=False)
            PU.append(p); pid[p['id']] = p
        if t['name'] not in names: names.append(t['name'])
        b = p['node']
        E.append(dict(u=a, v=b, n=names.index(t['name']), c='A', L=round(t['km'] * 1000), cap0=float(t['cap']), cap_osm=float(t['cap']),
                      w=0, st=None, tunnel=True, g=[[N[a]['lon'], N[a]['lat']], [N[b]['lon'], N[b]['lat']]]))
        out.append(dict(name=t['name'], cap=t['cap'], km=t['km'], area=t['area'], pump=p['name'], pump_cap=p['cap'],
                        intake_m=round(_d(N[a], lon, lat)), from_node=a, to_node=b))
    return out
