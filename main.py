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
from collections import defaultdict

# Couleurs console
from colorama import init, Fore, Style
init(autoreset=True)

# Notifications Windows (optionnelles)
from win10toast import ToastNotifier
toaster = ToastNotifier()

HISTORY_FILE = "ruban_history.json"

# ------------------------------------------------------------
# Chargement GTFS
# ------------------------------------------------------------
def load_gtfs_data(zip_path):
    with zipfile.ZipFile(zip_path, 'r') as z:
        required = ['routes.txt', 'trips.txt', 'stop_times.txt', 'stops.txt', 'calendar.txt']
        gtfs_data = {}
        for file in required:
            if file not in z.namelist():
                raise FileNotFoundError(f"Fichier manquant : {file}")
            df = pd.read_csv(z.open(file))
            gtfs_data[file.split('.')[0]] = df

        if 'calendar_dates.txt' in z.namelist():
            df = pd.read_csv(z.open('calendar_dates.txt'))
            df['date'] = pd.to_numeric(df['date'], errors='coerce').fillna(0).astype(int)
            gtfs_data['calendar_dates'] = df
        else:
            gtfs_data['calendar_dates'] = pd.DataFrame(columns=['service_id', 'date', 'exception_type'])

        cal = gtfs_data['calendar']
        cal['start_date'] = pd.to_numeric(cal['start_date'], errors='coerce').fillna(0).astype(int)
        cal['end_date'] = pd.to_numeric(cal['end_date'], errors='coerce').fillna(0).astype(int)

        return gtfs_data

# ------------------------------------------------------------
# Services actifs & courses en cours
# ------------------------------------------------------------
def get_active_service_ids(gtfs_data, today_date):
    calendar = gtfs_data['calendar']
    calendar_dates = gtfs_data['calendar_dates']
    ymd = int(today_date.strftime('%Y%m%d'))

    active = set()
    weekday = today_date.strftime('%A').lower()

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

        if start_dt.time().hour < 5 and now_datetime.time().hour >= 18:
            start_dt -= timedelta(days=1)
            end_dt -= timedelta(days=1)

        if start_dt <= now_datetime <= end_dt + timedelta(minutes=15):
            scheduled.add(row['trip_id'])

    return scheduled

# ------------------------------------------------------------
# Utilitaires temps & prochain arrêt
# ------------------------------------------------------------
def parse_gtfs_time(time_str, base_datetime):
    if pd.isna(time_str):
        return None
    hours, minutes, seconds = map(int, str(time_str).strip().split(':'))
    return base_datetime + timedelta(hours=hours, minutes=minutes, seconds=seconds)

def get_next_stop(trip_id, current_stop_sequence, gtfs_data):
    stop_times = gtfs_data['stop_times']
    stops = gtfs_data['stops']
    
    trip_stops = stop_times[stop_times['trip_id'] == trip_id].sort_values('stop_sequence')
    
    if trip_stops.empty or current_stop_sequence is None:
        return "Inconnu"
    
    next_seq = current_stop_sequence + 1
    next_row = trip_stops[trip_stops['stop_sequence'] == next_seq]
    
    if not next_row.empty:
        stop_id = next_row.iloc[0]['stop_id']
        name_row = stops[stops['stop_id'] == stop_id]
        if not name_row.empty:
            return name_row.iloc[0]['stop_name']
    
    if current_stop_sequence == trip_stops['stop_sequence'].max():
        return "Terminus atteint"
    
    return "Prochain arrêt inconnu"

def estimate_delay_from_position(trip_id, stop_sequence, position_time, now_datetime, gtfs_data):
    stop_times = gtfs_data['stop_times']
    row = stop_times[(stop_times['trip_id'] == trip_id) & (stop_times['stop_sequence'] == stop_sequence)]
    if row.empty:
        return 0

    scheduled_str = row.iloc[0]['arrival_time'] if pd.notna(row.iloc[0]['arrival_time']) else row.iloc[0]['departure_time']
    base = datetime.combine(now_datetime.date(), datetime.min.time())
    scheduled_dt = parse_gtfs_time(scheduled_str, base)
    if scheduled_dt > now_datetime + timedelta(hours=3):
        scheduled_dt -= timedelta(days=1)

    return int((position_time - scheduled_dt).total_seconds())

# ------------------------------------------------------------
# Récupération flux RT
# ------------------------------------------------------------
def fetch_gtfs_rt(url):
    headers = {'User-Agent': 'RubanMonitor/4.0'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

# ------------------------------------------------------------
# Analyse du flux realtime
# ------------------------------------------------------------
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

        status_code = v.current_status
        status_map = {0: "APPROCHE", 1: "À L'ARRÊT", 2: "EN ROUTE VERS"}
        status = status_map.get(status_code, "INCONNU")

        pos_time = datetime.fromtimestamp(v.timestamp) if v.HasField('timestamp') else now_datetime
        seq = v.current_stop_sequence if v.HasField('current_stop_sequence') else None

        occupancy_code = v.occupancy_status if v.HasField('occupancy_status') else None
        occupancy_map = {
            0: "VIDE", 1: "PEU DE PLACES LIBRES", 2: "PEU DE PLACES ASSISES",
            3: "BON NOMBRE DE PLACES", 4: "PRESQUE PLEIN", 5: "PLEIN DÉBOUT", 6: "COMPLET"
        }
        occupancy = occupancy_map.get(occupancy_code, "Non renseigné")

        delay_sec = 0
        if seq:
            delay_sec = estimate_delay_from_position(trip_id, seq, pos_time, now_datetime, gtfs_data)

        next_stop_name = get_next_stop(trip_id, seq, gtfs_data)

        vehicles[vehicle_id] = {
            'label': label,
            'route_id': route_id,
            'trip_id': trip_id,
            'status': status,
            'timestamp': pos_time,
            'delay_seconds': delay_sec,
            'occupancy': occupancy,
            'next_stop': next_stop_name
        }
        observed_trips.add(trip_id)
        vehicle_to_trips[label].add(trip_id)

    duplicates = {label: trips for label, trips in vehicle_to_trips.items() if len(trips) > 1}
    return vehicles, observed_trips, duplicates

# ------------------------------------------------------------
# Historique retard
# ------------------------------------------------------------
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# ------------------------------------------------------------
# Affichage
# ------------------------------------------------------------
def display_results(vehicles, observed_trips, scheduled_trips, gtfs_data, duplicates, history,
                    alert_only=False, follow_vehicle=None, beep_enabled=True, notifications_enabled=True):
    routes = gtfs_data['routes']
    trips = gtfs_data['trips']
    stop_times = gtfs_data['stop_times']
    now = datetime.now()

    missing = scheduled_trips - observed_trips

    # Log texte
    with open("ruban_log.txt", "a", encoding="utf-8") as f:
        f.write(f"{now.strftime('%Y-%m-%d %H:%M:%S')} | Véhicules: {len(vehicles)} | Absents: {len(missing)}\n")

    # Mise à jour historique retard
    line_delays = defaultdict(list)
    for info in vehicles.values():
        delay_min = info['delay_seconds'] // 60
        if delay_min > 0:
            line_delays[info['route_id']].append(delay_min)

    for route_id, delays in line_delays.items():
        avg = sum(delays) / len(delays) if delays else 0
        if route_id not in history:
            history[route_id] = []
        history[route_id].append((now.strftime('%H:%M'), avg))

    # Alertes
    has_problem = bool(missing or duplicates or any(abs(v['delay_seconds']) > 600 for v in vehicles.values()))
    if has_problem:
        if beep_enabled:
            print("\a")
        if notifications_enabled:
            toaster.show_toast("Ruban Alerte", f"{len(missing)} absent(s) • Retards • Doublons", duration=10, threaded=True)

    # Mode alert-only
    if alert_only:
        if has_problem:
            if missing:
                print(Fore.RED + Style.BRIGHT + f"🚨 {len(missing)} COURSE(S) ABSENTE(S)")
                for trip_id in sorted(missing):
                    t = trips[trips['trip_id'] == trip_id].iloc[0]
                    r = routes[routes['route_id'] == t['route_id']]
                    ligne = r['route_short_name'].iloc[0] if not r.empty else "?"
                    dest = t.get('trip_headsign', "?")
                    heure = stop_times[stop_times['trip_id'] == trip_id].sort_values('stop_sequence').iloc[0]['departure_time']
                    print(Fore.RED + f"   ❌ {ligne} | Départ {heure} → {dest}")
            if duplicates:
                print(Fore.MAGENTA + "⚡ DOUBLONS DÉTECTÉS")
                for label, trips in duplicates.items():
                    print(Fore.MAGENTA + f"   Véhicule {label} sur {len(trips)} courses !")
        else:
            print(Fore.GREEN + "✅ Tout est nominal")
        return

    # Affichage normal
    print(Fore.CYAN + Style.BRIGHT + f"\n=== {len(vehicles)} véhicule(s) en ligne à {now.strftime('%H:%M:%S')} ===\n")

    displayed = vehicles.values()
    if follow_vehicle:
        displayed = [v for v in displayed if v['label'] == follow_vehicle]
        if not displayed:
            print(Fore.YELLOW + f"Véhicule {follow_vehicle} non détecté.")
            return

    for info in sorted(displayed, key=lambda x: x['label']):
        trip_row = trips[trips['trip_id'] == info['trip_id']].iloc[0]
        dest = trip_row.get('trip_headsign', "?")

        route_row = routes[routes['route_id'] == info['route_id']]
        ligne = route_row['route_short_name'].iloc[0] if not route_row.empty else info['route_id']

        depart = stop_times[stop_times['trip_id'] == info['trip_id']].sort_values('stop_sequence').iloc[0]['departure_time']

        delay_sec = info['delay_seconds']
        delay_min = abs(delay_sec) // 60

        if delay_sec > 30:
            etat = Fore.RED + f"{delay_min} min de retard"
        elif delay_sec < -30:
            etat = Fore.GREEN + f"{delay_min} min d'avance"
        else:
            etat = Fore.GREEN + "à l'heure"

        print(Fore.WHITE + Style.BRIGHT + f"Véhicule {info['label']} → " + Fore.CYAN + f"Ligne {ligne}")
        print(f"   Destination : {dest}")
        print(f"   Départ prévu : {depart}")
        print(f"   Statut : {info['status']} | État : {etat}")
        print(Fore.MAGENTA + f"   Prochain arrêt : {info['next_stop']}")
        print(f"   Occupation : {info['occupancy']}")
        print(f"   Dernière position : {info['timestamp'].strftime('%H:%M:%S')}")
        print("-" * 70)

    # Historique retard moyen
    if history:
        print(Fore.CYAN + "📊 Retard moyen par ligne (session) :")
        for route_id, records in history.items():
            if records:
                avg = sum(r[1] for r in records) / len(records)
                ligne_name = routes[routes['route_id'] == route_id]['route_short_name'].iloc[0] if route_id in routes['route_id'].values else route_id
                color = Fore.GREEN if avg < 3 else Fore.YELLOW if avg < 8 else Fore.RED
                print(f"   {ligne_name} → {color}{avg:.1f} min")

    # Doublons & absences
    if duplicates:
        print(Fore.MAGENTA + Style.BRIGHT + "⚡ DOUBLONS DÉTECTÉS")
        for label, trips in duplicates.items():
            print(Fore.MAGENTA + f"   Véhicule {label} sur {len(trips)} courses !")

    if missing:
        print(Fore.RED + Style.BRIGHT + f"🚨 {len(missing)} course(s) ABSENTE(S)")
        for trip_id in sorted(missing):
            t = trips[trips['trip_id'] == trip_id].iloc[0]
            r = routes[routes['route_id'] == t['route_id']]
            ligne = r['route_short_name'].iloc[0] if not r.empty else "?"
            dest = t.get('trip_headsign', "?")
            heure = stop_times[stop_times['trip_id'] == trip_id].sort_values('stop_sequence').iloc[0]['departure_time']
            print(Fore.RED + f"   ❌ {ligne} | Départ {heure} → {dest}")
    else:
        print(Fore.GREEN + Style.BRIGHT + "✓ Toutes les courses sont détectées")

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moniteur Ruban - Version finale complète")
    parser.add_argument("zip_path", help="Chemin vers le fichier GTFS.zip")
    parser.add_argument("rt_url", help="URL du flux GTFS-Realtime")
    parser.add_argument("--interval", type=int, default=60, help="Intervalle de rafraîchissement en secondes (défaut: 60)")
    parser.add_argument("--alert-only", action="store_true", help="Afficher uniquement les alertes")
    parser.add_argument("--follow", type=str, help="Suivre un seul véhicule (numéro)")
    parser.add_argument("--no-notif", action="store_true", help="Désactiver les notifications Windows")
    parser.add_argument("--no-beep", action="store_true", help="Désactiver le beep console")
    args = parser.parse_args()

    history = load_history()

    while True:
        now = datetime.now()
        print(Fore.WHITE + Style.BRIGHT + f"Analyse Ruban — {now.strftime('%d/%m/%Y %H:%M:%S')}")

        try:
            gtfs_data = load_gtfs_data(args.zip_path)
            feed = fetch_gtfs_rt(args.rt_url)
            vehicles, observed, duplicates = analyze_realtime_feed(feed, now, gtfs_data)
            scheduled = get_scheduled_trips_now(gtfs_data, now)
            display_results(
                vehicles, observed, scheduled, gtfs_data, duplicates, history,
                alert_only=args.alert_only,
                follow_vehicle=args.follow,
                beep_enabled=not args.no_beep,
                notifications_enabled=not args.no_notif
            )

            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False)

        except Exception as e:
            print(Fore.RED + f"Erreur : {e}")
            import traceback
            traceback.print_exc()

        if args.interval > 0:
            print(f"\nProchaine mise à jour dans {args.interval} secondes...\n")
            time.sleep(args.interval)
