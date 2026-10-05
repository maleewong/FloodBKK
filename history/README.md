# FloodBKK: ข้อมูลตรวจวัดที่บันทึกไว้ (data/history)

บันทึกโดย `scripts/update.py` ทุกครั้งที่อัปเดต (ทุกชั่วโมง) ไม่เขียนทับ ไม่บันทึกซ้ำ
เวลาเป็นเวลาไทย (UTC+7) รูปแบบ `YYYY-MM-DD HH:MM` ไฟล์ CSV เป็น UTF-8 (มี BOM เปิดใน Excel ได้)
แหล่งข้อมูล: สำนักการระบายน้ำ กทม. (weather.bangkok.go.th), Traffy Fondue, Open-Meteo
ไฟล์ CSV ถูก push ขึ้น GitHub (โฟลเดอร์ `history/` ใน repo) ทุกชั่วโมงด้วย ส่วน `snap/` และ `forecast/` (gzip) อยู่ในเครื่องเท่านั้น

## ค่าตรวจวัด (แยกไฟล์รายเดือน `<ชนิด>_YYYY-MM.csv`)

| ไฟล์ | หนึ่งแถว | คอลัมน์ |
|---|---|---|
| `canal_*.csv` | สถานีวัดน้ำคลอง × เวลาที่วัด | code, measured, wl_in_m (ระดับน้ำด้านใน ม.รทก.), wl_out_m, wl_out2_m (ด้านนอกประตู), gate1–3 (การเปิดบานประตู), status (Normal/Alert/Critical/out of order), max_in_today_m |
| `road_*.csv` | เซนเซอร์น้ำท่วมถนน × เวลา | code, measured, depth_cm, max_cm, status (Normal/Minor flooding/Flood/Out of order) |
| `rain_*.csv` | สถานีวัดฝน × เวลา | code, measured, rf15m_mm, rf1h_mm, rf3h_mm, rf6h_mm, rf12h_mm, rf24h_mm (ฝนสะสมย้อนหลัง) |
| `tunnel_*.csv` | อุโมงค์ทางลอด × เวลา | code, measured, depth_cm, max_cm |
| `traffy_*.csv` (ตามเดือนที่แจ้ง) | เรื่องแจ้ง × สถานะ | ticket, state, reported, last_activity, lon, lat, district, subdistrict, description, photo_url (ประเภท น้ำท่วม; สถานะเปลี่ยนจะได้แถวใหม่) |
| `pump_daily.csv` | สถานีสูบ × วัน | day, code, name, capacity_m3 (กำลังสูบรวมทั้งวัน), discharged_m3 (สูบจริง) |

## ข้อมูลสถานี (เพิ่มแถวเมื่อมีอะไรเปลี่ยน เช่น เกณฑ์ ตลิ่ง พิกัด)

| ไฟล์ | คอลัมน์ |
|---|---|
| `canal_stations.csv` | code, name, district_id, district, lat, lon, system, gates, left_bank_m, right_bank_m, warning_m, critical_m, warning_out_m, critical_out_m, river, first_seen |
| `road_sensors.csv` | code, name, road, district_id, district, lat, lon, type, first_seen |
| `rain_stations.csv` | code, name, district_id, district, lat, lon, first_seen |

## สิ่งที่โมเดลแนะนำในแต่ละรอบ (สถานะ → การกระทำ สำหรับ reinforcement learning / ประเมินคำแนะนำย้อนหลัง)

| ไฟล์ | คอลัมน์ |
|---|---|
| `advice_pumps_*.csv` | run, scenario (forecast/extreme), pump, measured_m3s (สูบจริงเฉลี่ย 3 วัน), advised_first_m3s, advised_max_m3s, start_step (ช่วง 3 ชม.), capacity_m3s |
| `advice_plan_*.csv` | run, scenario (current/plan/mobile/trunk), zone, days_50, days_90 (วันที่ระบายได้ 50/90%), volume0_mm3, lambda |

## ไฟล์ gzip (อยู่ในเครื่อง)

- `snap/YYYYMMDD_HH.json.gz`: ข้อมูลดิบทั้งชุดของรอบนั้น (คลอง ถนน ฝน อุโมงค์) ราว 26 KB
- `forecast/YYYYMMDD_HH.json.gz`: ฝนพยากรณ์ Open-Meteo ที่ใช้ในรอบนั้น (25 จุด × 72 ชม.) ใช้เทียบกับฝนที่ตกจริง

## ข้อมูลกายภาพ (ไม่เปลี่ยน อยู่ใน data/)

- `network.json`: โครงข่ายคลองจาก OpenStreetMap (จุดต่อ ช่วงคลอง ความยาว ความกว้าง พื้นที่รับน้ำ สถานีสูบ ทางออก)
- `dem_ground.npy`, `dem_raw.npy`, `dem_meta.json`: ความสูงพื้นดิน/ผิวบนสุด (Copernicus 30 ม. รวมเป็นช่อง 120 ม.)
- `bkk_districts.geojson`: ขอบเขต 50 เขต
- `canal_widths2557.json`, `pump_report2557.json`, `control_levels2557.json`: จากแผนปฏิบัติการฯ กทม. 2557
- `road_net.json`, `road_segments.json`, `osm_residential.json`: ถนนและหมู่บ้านจาก OpenStreetMap
- `scripts/tunnels.py`: อุโมงค์ระบายน้ำ 5 แห่ง (ความจุ จุดรับน้ำ ทางออก แหล่งอ้างอิง)
