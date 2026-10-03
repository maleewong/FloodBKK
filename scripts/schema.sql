-- Bangkok drainage prototype database (source: DDS/BMA weather.bangkok.go.th). Times are local (UTC+7).
drop table if exists canal_station; drop table if exists canal_snapshot; drop table if exists canal_hourly;
drop table if exists road_sensor; drop table if exists road_snapshot; drop table if exists tunnel_snapshot;
drop table if exists rain_station; drop table if exists rain_snapshot; drop table if exists rain_daily_top50;
drop table if exists pump_daily; drop table if exists road_flood_event;

create table canal_station(code text primary key, name text, district_id int, district text, in_bkk int,
  lat real, lon real, system int, is_gate int, river text, left_bank_m real, right_bank_m real,
  warning_m real, critical_m real, warning_out_m real, critical_out_m real);
create table canal_snapshot(code text, fetched text, measured text, status text, wl_in_m real, wl_out_m real,
  wl_out2_m real, gate1 real, gate2 real, gate3 real, max_in_today_m real, max_in_yesterday_m real);
create table canal_hourly(code text, ts text, wl_in_m real, wl_out_m real, wl_river_m real, primary key(code, ts));
create table road_sensor(code text primary key, name text, road text, district_id int, district text, lat real, lon real);
create table road_snapshot(code text, fetched text, measured text, depth_cm real, status text);
create table tunnel_snapshot(code text, name text, lat real, lon real, fetched text, measured text, depth_cm real);
create table rain_station(code text primary key, name text, district_id int, district text, lat real, lon real);
create table rain_snapshot(code text, fetched text, measured text, rf15m real, rf1h real, rf3h real, rf6h real, rf12h real, rf24h real);
create table rain_daily_top50(day text, code text, district text, name text, rf24h_mm real, primary key(day, code));
create table pump_daily(day text, code text, name text, capacity_m3 real, discharged_m3 real, primary key(day, code));
create table road_flood_event(code text, road text, start text, end text, hours real, primary key(code, start));
