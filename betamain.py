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
from colorama import init, Fore, Style
import csv

try:
    import folium
except ImportError:
    folium = None

init(autoreset=True)

HISTORY_FILE = "ruban_history.json"
LOG_FILE = "ruban_log.txt"
STATS_FILE = "ruban_stats.csv"
MAP_FILE = "ruban_map.html"

# =========================
# Chargement GTFS statique
# =========================

def load_gtfs_data(zip_path):
    """Charge les données GTFS depuis un fichier zip et retourne un dictionnaire de DataFrames."""
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
                gtfs_data['calendar_dates']['date'] = pd.to_numeric(
                    gtfs_data['calendar_dates']['date'], errors='coerce'
                ).fillna(0).astype(int)
            else:
                gtfs_data['calendar_dates'] = pd.DataFrame(columns=['service_id', 'date', 'exception_type'])

            gtfs_data['calendar']['start_date'] = pd.to_numeric(
                gtfs_data['calendar']['start_date'], errors='coerce'
            ).fillna(0).astype(int)
            gtfs_data['calendar']['end_date'] = pd.to_numeric(
                gtfs_data['calendar']['end_date'], errors='coerce'
            ).fillna(0).astype(int)

            return gtfs_data
    except Exception as e:
        print(Fore.RED + f"Erreur lors du chargement du GTFS : {e}")
        raise

def get_active_service_ids(gtfs_data, today_date):
    """Retourne les IDs des services actifs pour la date donnée."""
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
    """Convertit une chaîne GTFS (HH:MM:SS) en datetime."""
    if pd.isna(time_str):
        return None
    hours, minutes, seconds = map(int, str(time_str).strip().split(':'))
    return base_datetime + timedelta(hours=hours, minutes=minutes, seconds=seconds)

def get_scheduled_trips_now(gtfs_data, now_datetime):
    """Retourne les trips prévus à l'heure actuelle."""
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

# =========================
# Outils horaires / retards
# =========================

def get_next_stop(trip_id, current_stop_sequence, gtfs_data):
    """Retourne le nom du prochain arrêt pour un trip donné."""
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
    """Estime le retard en secondes pour un arrêt donné."""
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

# =========================
# GTFS-RT
# =========================

def fetch_gtfs_rt(url):
    """Récupère le flux GTFS-Realtime depuis une URL."""
    headers = {'User-Agent': 'RubanMonitor/4.0'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

def load_gtfs_rt_from_file(path):
    """Charge un flux GTFS-RT depuis un fichier binaire (mode relecture/simulation)."""
    feed = gtfs_realtime_pb2.FeedMessage()
    with open(path, "rb") as f:
        feed.ParseFromString(f.read())
    return feed

def analyze_realtime_feed(feed, now_datetime, gtfs_data):
    """Analyse le flux GTFS-Realtime et retourne les véhicules, trips observés et doublons."""
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
        if seq is not None:
            delay_sec = estimate_delay_from_position(trip_id, seq, pos_time, now_datetime, gtfs_data)

        next_stop_name = get_next_stop(trip_id, seq, gtfs_data)

        lat = v.position.latitude if v.HasField('position') and v.position.HasField('latitude') else None
        lon = v.position.longitude if v.HasField('position') and v.position.HasField('longitude') else None

        vehicles[vehicle_id] = {
            'label': label,
            'route_id': route_id,
            'trip_id': trip_id,
            'status': status,
            'timestamp': pos_time,
            'delay_seconds': delay_sec,
            'occupancy': occupancy,
            'next_stop': next_stop_name,
            'lat': lat,
            'lon': lon
        }
        observed_trips.add(trip_id)
        vehicle_to_trips[label].add(trip_id)

    duplicates = {label: trips for label, trips in vehicle_to_trips.items() if len(trips) > 1}
    return vehicles, observed_trips, duplicates

# =========================
# Historique / logs / stats
# =========================

def load_history():
    """Charge l'historique des retards depuis un fichier JSON."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(history):
    """Sauvegarde l'historique des retards dans un fichier JSON."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

def log_to_file(message):
    """Écrit un message dans le fichier de log."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")

def append_stats(stats_rows, filename=STATS_FILE):
    """Ajoute des lignes de stats dans un CSV."""
    file_exists = os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        if not file_exists:
            writer.writerow(["datetime", "route_id", "avg_delay_min", "nb_vehicules"])
        for row in stats_rows:
            writer.writerow(row)

def compute_line_stats(vehicles):
    """Retourne un dict route_id -> (avg_delay_min, nb_vehicules)."""
    line_delays = defaultdict(list)
    for info in vehicles.values():
        delay_min = info['delay_seconds'] / 60.0
        line_delays[info['route_id']].append(delay_min)

    stats = {}
    for route_id, delays in line_delays.items():
        if delays:
            stats[route_id] = (sum(delays) / len(delays), len(delays))
    return stats

def detect_anomalies(vehicles, history, threshold_minutes=10):
    """Détection d'anomalies : retards très élevés ou très supérieurs à l'historique."""
    anomalies = []
    for v in vehicles.values():
        route_id = v['route_id']
        delay_min = v['delay_seconds'] / 60.0
        if abs(delay_min) >= threshold_minutes:
            anomalies.append((v, "Retard/avance très important"))

        if route_id in history and history[route_id]:
            avg_hist = sum(r[1] for r in history[route_id]) / len(history[route_id])
            if abs(delay_min) > 2 * abs(avg_hist) and abs(delay_min) > 3:
                anomalies.append((v, "Retard anormal par rapport à l'historique"))
    return anomalies

def predict_delays(history):
    """Prédiction simple : moyenne historique par ligne."""
    predictions = {}
    for route_id, records in history.items():
        if records:
            avg = sum(r[1] for r in records) / len(records)
            predictions[route_id] = avg
    return predictions

# =========================
# Visualisation / carte
# =========================

def generate_map(vehicles, gtfs_data, filename=MAP_FILE):
    """Génère une carte HTML avec les véhicules en temps réel."""
    if folium is None:
        print(Fore.YELLOW + "Folium non installé, carte non générée.")
        return

    coords = [(v['lat'], v['lon']) for v in vehicles.values() if v['lat'] is not None and v['lon'] is not None]
    if not coords:
        print(Fore.YELLOW + "Aucune coordonnée disponible pour générer la carte.")
        return

    avg_lat = sum(c[0] for c in coords) / len(coords)
    avg_lon = sum(c[1] for c in coords) / len(coords)

    m = folium.Map(location=[avg_lat, avg_lon], zoom_start=12)
    routes = gtfs_data['routes']
    trips = gtfs_data['trips']

    for v in vehicles.values():
        if v['lat'] is None or v['lon'] is None:
            continue
        trip_row = trips[trips['trip_id'] == v['trip_id']]
        dest = trip_row['trip_headsign'].iloc[0] if not trip_row.empty else "?"
        route_row = routes[routes['route_id'] == v['route_id']]
        ligne = route_row['route_short_name'].iloc[0] if not route_row.empty else v['route_id']
        popup = f"{v['label']} - Ligne {ligne}<br>Dest : {dest}<br>Statut : {v['status']}<br>Retard : {v['delay_seconds']/60:.1f} min"
        folium.Marker(
            location=[v['lat'], v['lon']],
            popup=popup,
            tooltip=f"Ligne {ligne}"
        ).add_to(m)

    m.save(filename)
    print(Fore.CYAN + f"Carte générée : {filename}")

# =========================
# Alertes
# =========================

def send_webhook_alert(url, payload):
    """Envoie une alerte JSON vers un webhook (Discord, etc.)."""
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(Fore.YELLOW + f"Échec envoi webhook : {e}")

# =========================
# Affichage / suivi
# =========================

def display_results(
    vehicles,
    observed_trips,
    scheduled_trips,
    gtfs_data,
    duplicates,
    history,
    alert_only=False,
    follow_vehicle=None,
    beep_enabled=True,
    alert_threshold=10,
    webhook_url=None,
    vehicle_tracks=None,
    enable_stats_export=False
):
    """Affiche les résultats de l'analyse du flux GTFS-Realtime et gère stats/alertes."""
    routes = gtfs_data['routes']
    trips = gtfs_data['trips']
    stop_times = gtfs_data['stop_times']
    now = datetime.now()

    missing = scheduled_trips - observed_trips
    log_to_file(f"Véhicules: {len(vehicles)} | Absents: {len(missing)}")

    # Mise à jour historique par ligne
    line_stats = compute_line_stats(vehicles)
    for route_id, (avg_delay, nb) in line_stats.items():
        if route_id not in history:
            history[route_id] = []
        history[route_id].append((now.strftime('%H:%M'), avg_delay))

    # Export CSV si demandé
    if enable_stats_export and line_stats:
        rows = []
        for route_id, (avg_delay, nb) in line_stats.items():
            rows.append([now.strftime('%Y-%m-%d %H:%M:%S'), route_id, f"{avg_delay:.2f}", nb])
        append_stats(rows)

    # Anomalies
    anomalies = detect_anomalies(vehicles, history, threshold_minutes=alert_threshold)

    has_problem = bool(
        missing or duplicates or anomalies
    )
    if has_problem and beep_enabled:
        print("\a")

    # Webhook si problème
    if has_problem and webhook_url:
        payload = {
            "timestamp": now.isoformat(),
            "missing_trips": list(missing),
            "duplicates": {k: list(v) for k, v in duplicates.items()},
            "anomalies": [
                {
                    "vehicle": a[0]['label'],
                    "route_id": a[0]['route_id'],
                    "delay_min": a[0]['delay_seconds'] / 60.0,
                    "reason": a[1]
                } for a in anomalies
            ]
        }
        send_webhook_alert(webhook_url, payload)

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
                for label, trips_dup in duplicates.items():
                    print(Fore.MAGENTA + f"   Véhicule {label} sur {len(trips_dup)} courses !")
            if anomalies:
                print(Fore.YELLOW + "⚠ Anomalies détectées :")
                for v, reason in anomalies:
                    print(Fore.YELLOW + f"   {v['label']} (ligne {v['route_id']}) → {reason} ({v['delay_seconds']/60:.1f} min)")
        else:
            print(Fore.GREEN + "✅ Tout est nominal")
        return

    print(Fore.CYAN + Style.BRIGHT + f"\n=== {len(vehicles)} véhicule(s) en ligne à {now.strftime('%H:%M:%S')} ===\n")

    displayed = vehicles.values()
    if follow_vehicle:
        displayed = [v for v in displayed if v['label'] == follow_vehicle]
        if not displayed:
            print(Fore.YELLOW + f"Véhicule {follow_vehicle} non détecté.")
            return

    # Suivi des trajectoires
    if vehicle_tracks is not None:
        for vid, v in vehicles.items():
            if v['lat'] is not None and v['lon'] is not None:
                vehicle_tracks[vid].append((now.isoformat(), v['lat'], v['lon']))

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
        if info['lat'] is not None and info['lon'] is not None:
            print(f"   Position : {info['lat']:.5f}, {info['lon']:.5f}")
        print("-" * 70)

    # Stats session
    if history:
        print(Fore.CYAN + "📊 Retard moyen par ligne (session) :")
        for route_id, records in history.items():
            if records:
                avg = sum(r[1] for r in records) / len(records)
                ligne_name = routes[routes['route_id'] == route_id]['route_short_name'].iloc[0] if route_id in routes['route_id'].values else route_id
                color = Fore.GREEN if avg < 3 else Fore.YELLOW if avg < 8 else Fore.RED
                print(f"   {ligne_name} → {color}{avg:.1f} min")

    # Prédiction simple
    predictions = predict_delays(history)
    if predictions:
        print(Fore.CYAN + "🔮 Prédiction simple du retard moyen par ligne :")
        for route_id, avg in predictions.items():
            ligne_name = routes[routes['route_id'] == route_id]['route_short_name'].iloc[0] if route_id in routes['route_id'].values else route_id
            print(f"   {ligne_name} → {avg:.1f} min attendues")

    if duplicates:
        print(Fore.MAGENTA + Style.BRIGHT + "⚡ DOUBLONS DÉTECTÉS")
        for label, trips_dup in duplicates.items():
            print(Fore.MAGENTA + f"   Véhicule {label} sur {len(trips_dup)} courses !")

    if anomalies:
        print(Fore.YELLOW + Style.BRIGHT + "⚠ Anomalies détectées :")
        for v, reason in anomalies:
            print(Fore.YELLOW + f"   {v['label']} (ligne {v['route_id']}) → {reason} ({v['delay_seconds']/60:.1f} min)")

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

# =========================
# Main / arguments
# =========================

def main():
    parser = argparse.ArgumentParser(description="Moniteur Ruban - Version enrichie (Linux, stats, carte, simulation)")
    parser.add_argument("zip_path", help="Chemin vers le fichier GTFS.zip")
    parser.add_argument("rt_source", help="URL du flux GTFS-Realtime ou dossier de relecture (voir --replay-dir)")
    parser.add_argument("--interval", type=int, default=60, help="Intervalle de rafraîchissement en secondes (défaut: 60)")
    parser.add_argument("--alert-only", action="store_true", help="Afficher uniquement les alertes")
    parser.add_argument("--follow", type=str, help="Suivre un seul véhicule (numéro)")
    parser.add_argument("--no-beep", action="store_true", help="Désactiver le beep console")
    parser.add_argument("--alert-threshold", type=int, default=10, help="Seuil d'anomalie en minutes (retard/avance)")
    parser.add_argument("--webhook-url", type=str, help="URL webhook pour envoyer des alertes JSON")
    parser.add_argument("--map", action="store_true", help="Générer une carte HTML des véhicules")
    parser.add_argument("--stats-csv", action="store_true", help="Exporter les stats de retard dans un CSV")
    parser.add_argument("--replay-dir", action="store_true", help="Interpréter rt_source comme un dossier de fichiers GTFS-RT pour relecture/simulation")
    parser.add_argument("--simulation-speed", type=float, default=1.0, help="Facteur de vitesse en mode relecture (1.0 = temps réel, 2.0 = 2x plus rapide, etc.)")

    args = parser.parse_args()

    history = load_history()
    vehicle_tracks = defaultdict(list)

    if args.replay_dir:
        # Mode relecture / simulation : rt_source est un dossier contenant des fichiers .pb
        folder = args.rt_source
        files = sorted(
            [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".pb")]
        )
        if not files:
            print(Fore.RED + f"Aucun fichier .pb trouvé dans {folder}")
            sys.exit(1)

        print(Fore.CYAN + f"Mode relecture : {len(files)} fichiers trouvés dans {folder}")
        previous_time = None

        for path in files:
            now = datetime.now()
            print(Fore.WHITE + Style.BRIGHT + f"Analyse Ruban (relecture) — fichier {os.path.basename(path)}")

            try:
                gtfs_data = load_gtfs_data(args.zip_path)
                feed = load_gtfs_rt_from_file(path)
                vehicles, observed, duplicates = analyze_realtime_feed(feed, now, gtfs_data)
                scheduled = get_scheduled_trips_now(gtfs_data, now)

                display_results(
                    vehicles, observed, scheduled, gtfs_data, duplicates, history,
                    alert_only=args.alert_only,
                    follow_vehicle=args.follow,
                    beep_enabled=not args.no_beep,
                    alert_threshold=args.alert_threshold,
                    webhook_url=args.webhook_url,
                    vehicle_tracks=vehicle_tracks,
                    enable_stats_export=args.stats_csv
                )

                save_history(history)

                if args.map:
                    generate_map(vehicles, gtfs_data)

            except Exception as e:
                print(Fore.RED + f"Erreur : {e}")
                import traceback
                traceback.print_exc()

            # Simulation temporelle simple : pause basée sur l'intervalle demandé et le facteur de vitesse
            if args.interval > 0:
                sleep_time = args.interval / max(args.simulation_speed, 0.1)
                print(f"\nProchaine mise à jour (relecture) dans {sleep_time:.1f} secondes...\n")
                time.sleep(sleep_time)

    else:
        # Mode temps réel classique
        while True:
            now = datetime.now()
            print(Fore.WHITE + Style.BRIGHT + f"Analyse Ruban — {now.strftime('%d/%m/%Y %H:%M:%S')}")

            try:
                gtfs_data = load_gtfs_data(args.zip_path)
                feed = fetch_gtfs_rt(args.rt_source)
                vehicles, observed, duplicates = analyze_realtime_feed(feed, now, gtfs_data)
                scheduled = get_scheduled_trips_now(gtfs_data, now)

                display_results(
                    vehicles, observed, scheduled, gtfs_data, duplicates, history,
                    alert_only=args.alert_only,
                    follow_vehicle=args.follow,
                    beep_enabled=not args.no_beep,
                    alert_threshold=args.alert_threshold,
                    webhook_url=args.webhook_url,
                    vehicle_tracks=vehicle_tracks,
                    enable_stats_export=args.stats_csv
                )

                save_history(history)

                if args.map:
                    generate_map(vehicles, gtfs_data)

            except Exception as e:
                print(Fore.RED + f"Erreur : {e}")
                import traceback
                traceback.print_exc()

            if args.interval > 0:
                print(f"\nProchaine mise à jour dans {args.interval} secondes...\n")
                time.sleep(args.interval)

if __name__ == "__main__":
    main()
