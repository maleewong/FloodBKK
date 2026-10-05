# FloodBKK: ข้อมูลตรวจวัดที่บันทึกไว้ (data/history)

บันทึกโดย `scripts/update.py` ทุกครั้งที่อัปเดต (ทุกชั่วโมง) ไม่เขียนทับ ไม่บันทึกซ้ำ
เวลาเป็นเวลาไทย (UTC+7) รูปแบบ `YYYY-MM-DD HH:MM` ไฟล์ CSV เป็น UTF-8 (มี BOM เปิดใน Excel ได้)
แหล่งข้อมูล: สำนักการระบายน้ำ กทม. (weather.bangkok.go.th), กรมชลประทาน (hyd-app-db.rid.go.th), Traffy Fondue, Open-Meteo
บน VPS มี 3 งาน: `update.py` ทุกชั่วโมง (นาที 20, โมเดล + หน้าเว็บ), `collect.py` ทุก 10 นาที (เก็บค่าตรวจวัดอย่างเดียว),
`fetch_history.py` ทุกคืน 01:40 (ดึงข้อมูลย้อนหลังที่เว็บต้นทางเก็บไว้ ไม่เกิน 4 ชม.)
ไฟล์หลักเป็นรายเดือน (`<ชนิด>_YYYY-MM.csv`) และแยกเป็นรายวันใน `daily/<ชนิด>/<ชนิด>_YYYY-MM-DD.csv` เมื่อวันนั้นจบ
GitHub เก็บเฉพาะ `daily/` + ข้อมูลสถานี + pump_daily.csv (โฟลเดอร์ `history/` ใน repo) ส่วนไฟล์รายเดือน `snap/` และ `forecast/` อยู่ในเครื่องเท่านั้น

ช่วงที่เครื่องปิด: รอบถัดไปดึงระดับน้ำคลองรายชั่วโมงย้อนหลังได้ราว 48 ชม. จากหน้าสถานีของ สนน. (status = `backfill`)
รายงาน Traffy ดึงย้อนหลัง 14 วันทุกรอบ ปริมาณสูบรายวันดึงย้อนหลัง 14 วัน ส่วนน้ำท่วมถนนและฝนรายชั่วโมงเติมย้อนหลังไม่ได้
`last_update.txt` = เวลาอัปเดตครั้งล่าสุด (ใช้ตรวจช่องว่าง)

## ค่าตรวจวัด (แยกไฟล์รายเดือน `<ชนิด>_YYYY-MM.csv`)

| ไฟล์ | หนึ่งแถว | คอลัมน์ |
|---|---|---|
| `canal_*.csv` | สถานีวัดน้ำคลอง × เวลาที่วัด | code, measured, wl_in_m (ระดับน้ำด้านใน ม.รทก.), wl_out_m, wl_out2_m (ด้านนอกประตู), gate1–3 (การเปิดบานประตู), status (Normal/Alert/Critical/out of order), max_in_today_m |
| `road_*.csv` | เซนเซอร์น้ำท่วมถนน × เวลา | code, measured, depth_cm, max_cm, status (Normal/Minor flooding/Flood/Out of order) |
| `rain_*.csv` | สถานีวัดฝน × เวลา | code, measured, rf15m_mm, rf1h_mm, rf3h_mm, rf6h_mm, rf12h_mm, rf24h_mm (ฝนสะสมย้อนหลัง) |
| `tunnel_*.csv` | อุโมงค์ทางลอด × เวลา | code, measured, depth_cm, max_cm |
| `traffy_*.csv` (ตามเดือนที่แจ้ง) | เรื่องแจ้ง × สถานะ | ticket, state, reported, last_activity, lon, lat, district, subdistrict, description, photo_url (ประเภท น้ำท่วม; สถานะเปลี่ยนจะได้แถวใหม่) |
| `pump_daily.csv` | สถานีสูบ × วัน | day, code, name, capacity_m3 (กำลังสูบรวมทั้งวัน), discharged_m3 (สูบจริง) |
| `pump_hourly_*.csv` | สถานีสูบ (โทรมาตร 32 แห่ง) × ชั่วโมง | code, hour (ต้นชั่วโมง), name, capacity_m3 (กำลังสูบเต็มที่ใน 1 ชม.), discharged_m3 (สูบจริงในชั่วโมงนั้น) = การกระทำจริงสำหรับ RL |
| `river_*.csv` | สถานีกรมชลประทาน × ชั่วโมง | code, measured, wl_msl_m (ม.รทก.), q_m3s (น้ำไหลผ่าน), stage_m (ค่าบนไม้วัดของสถานีที่มีศูนย์ไม้วัด) |
| `runs_*.csv` | รอบการคำนวณ | run, code_version (ลายนิ้วมือโค้ดโมเดล), host, bma_fetched_utc, จำนวนสถานีที่มีค่า (canal/road/rain), ฝนพยากรณ์รวม, plan_v, pump_avail_default, lambda 3 โซน, run_seconds |

ขอบเขตน้ำนอกระบบคลอง: ระดับแม่น้ำเจ้าพระยาอยู่ใน `canal_*.csv` แล้ว คือ `wl_out_m` ของประตู/สถานีสูบริมแม่น้ำราว 30 แห่ง
(บางเขนใหม่ บางซื่อ สามเสน เทเวศร์ ... พระโขนง บางนา สำโรง) และ `WL.PKG.01` ปากคลองตลาด ส่วนระดับน้ำทะเลคือ `WL.BPU.01` บางปู
ประตูชายขอบด้านตะวันออก (คลองสิบสาม พระยาสุเรนทร์ สองสายใต้ หลวงแพ่ง สามวา) ให้ระดับน้ำที่ไหลเข้าจากนอก กทม.
`river_*.csv` เพิ่มน้ำเหนือ: C.2 นครสวรรค์, C.13 ท้ายเขื่อนเจ้าพระยา, C.29B บางไทร (น้ำไหลผ่านเข้า กทม.), C.22A ปากเกร็ด, C.12 สามเสน,
แม่น้ำป่าสัก (S.28 ท้ายเขื่อนป่าสักฯ S.26 ท้ายเขื่อนพระรามหก S.5) และท่าจีน (T.1 นครชัยศรี T.14 สามพราน)

## ข้อมูลสถานี (เพิ่มแถวเมื่อมีอะไรเปลี่ยน เช่น เกณฑ์ ตลิ่ง พิกัด)

| ไฟล์ | คอลัมน์ |
|---|---|
| `canal_stations.csv` | code, name, district_id, district, lat, lon, system, gates, left_bank_m, right_bank_m, warning_m, critical_m, warning_out_m, critical_out_m, river, first_seen |
| `road_sensors.csv` | code, name, road, district_id, district, lat, lon, type, first_seen |
| `rain_stations.csv` | code, name, district_id, district, lat, lon, first_seen |
| `river_stations.csv` | code, name, province, basin, bank_m, zero_gauge_m (ถ้ามี: ตลิ่งเป็นค่าบนไม้วัด ม.รทก. = ค่า + ศูนย์ไม้วัด), q_bankfull_m3s, first_seen |

## สิ่งที่โมเดลแนะนำในแต่ละรอบ (สถานะ → การกระทำ สำหรับ reinforcement learning / ประเมินคำแนะนำย้อนหลัง)

| ไฟล์ | คอลัมน์ |
|---|---|
| `advice_pumps_*.csv` | run, scenario (forecast/extreme), pump, measured_m3s (สูบจริงเฉลี่ย 3 วัน), advised_first_m3s, advised_max_m3s, start_step (ช่วง 3 ชม.), capacity_m3s |
| `advice_plan_*.csv` | run, scenario (current/plan/mobile/trunk), zone, days_50, days_90 (วันที่ระบายได้ 50/90%), volume0_mm3, lambda |

## ผลของโมเดลแต่ละรอบ (ใช้ประเมินความแม่นยำย้อนหลัง: ทำนาย vs วัดจริง)

`model/YYYYMMDD_HH_forecast.json.gz` (ระดับน้ำคาดการณ์ 48 ชม. ทุก 3 ชม. ของทุกสถานี โซน เครื่องสูบ ทั้งกรณีฝนพยากรณ์และฝนหนัก),
`_plan.json.gz` และ `_plan_v068.json.gz` (แผนระบาย 30 วัน), `_flood.json.gz` (แผนที่น้ำท่วม ทุก 3 ชม.) คือไฟล์เดียวกับที่หน้าเว็บใช้ในรอบนั้น
ค่าสมมติฐานทั้งหมดอยู่ในคีย์ `assumptions` ของแต่ละไฟล์ และ `runs_*.csv` บอกว่ารอบนั้นใช้โค้ดรุ่นไหน

## ค่าตรวจวัดละเอียด (gzip CSV รายวัน)

| ไฟล์ | ความถี่ | คอลัมน์ |
|---|---|---|
| `hires/canal_YYYY-MM-DD.csv.gz` | ทุก 10 นาที (collect.py) | code, measured, wl_in_m, wl_out_m, wl_out2_m, gate1–3, status |
| `hires/road_…`, `hires/rain_…`, `hires/tunnel_…` | ทุก 10 นาที | เหมือนไฟล์รายเดือน |
| `past/canal5m_YYYY-MM-DD.csv.gz` | ทุก 5 นาที (ดึงย้อนหลัง) | code, measured, wl_in_m, wl_out_m, wl_out2_m |
| `past/rain5m_YYYY-MM-DD.csv.gz` | ทุก 5 นาที (ดึงย้อนหลัง) | code, measured, rf5m_mm, rf15m_mm, rf30m_mm, rf1h_mm, rf3h_mm, rf6h_mm, rf12h_mm, rf24h_mm |

ไฟล์ gzip เปิดด้วย pandas ได้ตรง ๆ (`pd.read_csv('x.csv.gz')`) หนึ่งไฟล์อาจมีหลายช่วง gzip ต่อกัน ซึ่งอ่านเป็นไฟล์เดียวได้ตามปกติ
ข้อมูลย้อนหลังที่เว็บต้นทางเก็บไว้ (ตรวจ 5 ต.ค. 2569): สนน. ระดับน้ำคลองทุก 5 นาทีย้อนได้ถึงอย่างน้อยปี 2566 ฝนทุก 5 นาทีถึงอย่างน้อยปี 2567,
ปริมาณสูบรายวันถึงอย่างน้อยปี 2566 (เลือกช่วงเวลาได้ละเอียดถึง 15 นาที), น้ำท่วมถนนเป็นรายเหตุการณ์ปี 2550–2569 (หน้า Flood/Graph/Station ยังไม่ได้ดึง),
กรมชลประทานรายชั่วโมงย้อนได้ถึงอย่างน้อยปี 2568 ดึงฤดูอื่นด้วย `python scripts/fetch_history.py --from 2025-05-01 --to 2025-11-30`

## ไฟล์ gzip อื่น ๆ (อยู่ในเครื่อง)

- `snap/YYYYMMDD_HH.json.gz`: ข้อมูลดิบทั้งชุดของรอบนั้น (คลอง ถนน ฝน อุโมงค์) ราว 26 KB
- `forecast/YYYYMMDD_HH.json.gz`: ฝนพยากรณ์ Open-Meteo ที่ใช้ในรอบนั้น (25 จุด × 72 ชม.) ใช้เทียบกับฝนที่ตกจริง

## ข้อมูลกายภาพ (ไม่เปลี่ยน อยู่ใน data/)

- `network.json`: โครงข่ายคลองจาก OpenStreetMap (จุดต่อ ช่วงคลอง ความยาว ความกว้าง พื้นที่รับน้ำ สถานีสูบ ทางออก)
- `dem_ground.npy`, `dem_raw.npy`, `dem_meta.json`: ความสูงพื้นดิน/ผิวบนสุด (Copernicus 30 ม. รวมเป็นช่อง 120 ม.)
- `bkk_districts.geojson`: ขอบเขต 50 เขต
- `canal_widths2557.json`, `pump_report2557.json`, `control_levels2557.json`: จากแผนปฏิบัติการฯ กทม. 2557
- `road_net.json`, `road_segments.json`, `osm_residential.json`: ถนนและหมู่บ้านจาก OpenStreetMap
- `scripts/tunnels.py`: อุโมงค์ระบายน้ำ 5 แห่ง (ความจุ จุดรับน้ำ ทางออก แหล่งอ้างอิง)
