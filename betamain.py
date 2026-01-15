import argparse
import zipfile
import pandas as pd
import requests
from google.transit import gtfs_realtime_pb2
from datetime import datetime, timedelta
import sys
import time
import os
import json
import hashlib
from collections import defaultdict, deque
from colorama import init, Fore, Style
import csv

init(autoreset=True)

HISTORY_FILE = "ruban_history.json"
LOG_FILE = "ruban_log.txt"
CACHE_FILE = "gtfs_cache.json"

# ---------------- UTILS ---------------- #

def log_to_file(message):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")

def hash_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------- GTFS ---------------- #

def load_gtfs_data(zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            required = ['routes.txt', 'trips.txt', 'stop_times.txt', 'stops.txt', 'calendar.txt']
            gtfs_data = {}
            for file in required:
                if file not in z.namelist():
                    raise FileNotFoundError(f"Fichier manquant : {file}")
                gtfs_data[file.split('.')[0]] = pd.read_csv(z.open(file))

            if 'calendar_dates.txt' in z.namelist():
                gtfs_data['calendar_dates'] = pd.read_csv(z.open('calendar_dates.txt'))
                gtfs_data['calendar_dates']['date'] = pd.to_numeric(gtfs_data['calendar_dates']['date'], errors='coerce').fillna(0).astype(int)
            else:
                gtfs_data['calendar_dates'] = pd.DataFrame(columns=['service_id', 'date', 'exception_type'])

            gtfs_data['calendar']['start_date'] = pd.to_numeric(gtfs_data['calendar']['start_date'], errors='coerce').fillna(0).astype(int)
            gtfs_data['calendar']['end_date'] = pd.to_numeric(gtfs_data['calendar']['end_date'], errors='coerce').fillna(0).astype(int)

            return gtfs_data
    except Exception as e:
        print(Fore.RED + f"Erreur chargement GTFS : {e}")
        raise

def load_gtfs_cached(zip_path):
    file_hash = hash_file(zip_path)
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        if cache.get("hash") == file_hash:
            return None, cache.get("data"), True

    data = load_gtfs_data(zip_path)
    with open(CACHE_FILE, "w") as f:
        json.dump({"hash": file_hash, "data": "cached"}, f)
    return data, None, False

# ---------------- SERVICES ---------------- #

def get_active_service_ids(gtfs_data, today_date):
    calendar = gtfs_data['calendar']
    calendar_dates = gtfs_data['calendar_dates']
    ymd = int(today_date.strftime('%Y%m%d'))
    weekday = today_date.strftime('%A').lower()

    active = set()
    if weekday in calendar.columns:
        mask = (
            (calendar['start_date'] <= ymd) &
            (calendar['end_date'] >= ymd) &
            (calendar[weekday] == 1)
        )
        active = set(calendar[mask]['service_id'])

    if not calendar_dates.empty:
        exc = calendar_dates[calendar_dates['date'] == ymd]
        added = set(exc[exc['exception_type'] == 1]['service_id'])
        removed = set(exc[exc['exception_type'] == 2]['service_id'])
        active = (active - removed) | added

    return active

def parse_gtfs_time(time_str, base_datetime):
    if pd.isna(time_str):
        return None
    h, m, s = map(int, str(time_str).split(':'))
    return base_datetime + timedelta(hours=h, minutes=m, seconds=s)

def get_scheduled_trips_now(gtfs_data, now_datetime):
    trips = gtfs_data['trips']
    stop_times = gtfs_data['stop_times']
    today = now_datetime.date()
    services = get_active_service_ids(gtfs_data, today)
    if not services:
        return set()

    active_trips = trips[trips['service_id'].isin(services)]['trip_id']
    bounds = stop_times.groupby('trip_id').agg(
        start=('departure_time', 'min'),
        end=('arrival_time', 'max')
    ).reset_index()
    bounds = bounds[bounds['trip_id'].isin(active_trips)]

    scheduled = set()
    base = datetime.combine(today, datetime.min.time())

    for _, row in bounds.iterrows():
        start_dt = parse_gtfs_time(row['start'], base)
        end_dt = parse_gtfs_time(row['end'], base)
        if start_dt <= now_datetime <= end_dt + timedelta(minutes=15):
            scheduled.add(row['trip_id'])

    return scheduled

# ---------------- REALTIME ---------------- #

def fetch_gtfs_rt(url):
    headers = {'User-Agent': 'RubanMonitor/5.0'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

# ---------------- ANALYSIS ---------------- #

def estimate_delay_from_position(trip_id, stop_sequence, position_time, now_datetime, gtfs_data):
    stop_times = gtfs_data['stop_times']
    row = stop_times[(stop_times['trip_id'] == trip_id) & (stop_times['stop_sequence'] == stop_sequence)]
    if row.empty:
        return 0

    scheduled_str = row.iloc[0]['arrival_time']
    base = datetime.combine(now_datetime.date(), datetime.min.time())
    scheduled_dt = parse_gtfs_time(scheduled_str, base)
    return int((position_time - scheduled_dt).total_seconds())

def analyze_realtime_feed(feed, now_datetime, gtfs_data):
    vehicles = {}
    observed_trips = set()
    vehicle_to_trips = defaultdict(set)

    for entity in feed.entity:
        if not entity.HasField('vehicle'):
            continue
        v = entity.vehicle
        if not v.trip.HasField('trip_id'):
            continue

        trip_id = v.trip.trip_id
        vehicle_id = v.vehicle.id
        label = v.vehicle.label if v.vehicle.HasField('label') else vehicle_id
        route_id = v.trip.route_id if v.trip.HasField('route_id') else "?"

        pos_time = datetime.fromtimestamp(v.timestamp) if v.HasField('timestamp') else now_datetime
        seq = v.current_stop_sequence if v.HasField('current_stop_sequence') else None

        delay_sec = estimate_delay_from_position(trip_id, seq, pos_time, now_datetime, gtfs_data) if seq else 0

        lat = v.position.latitude if v.HasField('position') else None
        lon = v.position.longitude if v.HasField('position') else None

        vehicles[vehicle_id] = {
            'label': label,
            'route_id': route_id,
            'trip_id': trip_id,
            'timestamp': pos_time,
            'delay_seconds': delay_sec,
            'lat': lat,
            'lon': lon
        }

        observed_trips.add(trip_id)
        vehicle_to_trips[label].add(trip_id)

    duplicates = {label: trips for label, trips in vehicle_to_trips.items() if len(trips) > 1}
    return vehicles, observed_trips, duplicates

# ---------------- EXPORT ---------------- #

def export_csv(filename, vehicles):
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "vehicle_id", "line", "trip_id", "delay_seconds", "lat", "lon"])
        for vid, v in vehicles.items():
            writer.writerow([
                v['timestamp'], vid, v['route_id'], v['trip_id'],
                v['delay_seconds'], v['lat'], v['lon']
            ])

def export_json(filename, vehicles):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(vehicles, f, ensure_ascii=False, default=str, indent=2)

# ---------------- DISPLAY ---------------- #

def display_results(vehicles, observed_trips, scheduled_trips, duplicates, args):
    missing = scheduled_trips - observed_trips
    print(Fore.CYAN + f"\n🚦 {len(vehicles)} véhicules | ❌ {len(missing)} absents")

    # Filtres
    display = vehicles.values()
    if args.only_delayed:
        display = [v for v in display if v['delay_seconds'] > args.delay_threshold * 60]

    if args.follow_vehicle:
        display = [v for v in display if v['label'] == args.follow_vehicle]

    # Mode tableau
    if args.table:
        print(Fore.WHITE + f"{'ID':<8}{'Ligne':<8}{'Retard':<10}{'Latitude':<12}{'Longitude':<12}")
        for v in display:
            delay = f"{v['delay_seconds']//60} min"
            print(f"{v['label']:<8}{v['route_id']:<8}{delay:<10}{str(v['lat']):<12}{str(v['lon']):<12}")
        return

    for v in display:
        delay_min = v['delay_seconds'] // 60
        color = Fore.RED if delay_min > args.delay_threshold else Fore.GREEN
        print(color + f"🚍 {v['label']} | Ligne {v['route_id']} | Retard: {delay_min} min")

    if duplicates:
        print(Fore.MAGENTA + "⚡ DOUBLONS DÉTECTÉS")
        for label, trips in duplicates.items():
            print(Fore.MAGENTA + f"   Véhicule {label} sur {len(trips)} courses")

    if missing:
        print(Fore.RED + f"🚨 {len(missing)} courses absentes")

# ---------------- MAIN ---------------- #

def main():
    parser = argparse.ArgumentParser(description="Ruban Monitor 5.0 — Centre de supervision GTFS")
    parser.add_argument("zip_path", help="Fichier GTFS.zip")
    parser.add_argument("rt_url", help="URL GTFS-RT")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--delay-threshold", type=int, default=5, help="Seuil de retard en minutes")
    parser.add_argument("--follow-vehicle", type=str)
    parser.add_argument("--watch-line", type=str)
    parser.add_argument("--only-delayed", action="store_true")
    parser.add_argument("--table", action="store_true")
    parser.add_argument("--export-csv", type=str)
    parser.add_argument("--export-json", type=str)
    args = parser.parse_args()

    gtfs_data = load_gtfs_data(args.zip_path)

    while True:
        now = datetime.now()
        print(Fore.WHITE + Style.BRIGHT + f"\n🕒 {now.strftime('%d/%m/%Y %H:%M:%S')}")

        try:
            feed = fetch_gtfs_rt(args.rt_url)
            vehicles, observed, duplicates = analyze_realtime_feed(feed, now, gtfs_data)
            scheduled = get_scheduled_trips_now(gtfs_data, now)

            display_results(vehicles, observed, scheduled, duplicates, args)

            if args.export_csv:
                export_csv(args.export_csv, vehicles)
            if args.export_json:
                export_json(args.export_json, vehicles)

        except Exception as e:
            print(Fore.RED + f"Erreur : {e}")

        time.sleep(args.interval)

if __name__ == "__main__":
    main()
