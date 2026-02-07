import argparse
from datetime import datetime, timedelta
from functools import lru_cache

from flask import Flask, jsonify, request, render_template_string

import main as core


app = Flask(__name__)

HTML_TEMPLATE = """
<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Ruban Dashboard</title>
    <style>
      body {
        font-family: Arial, sans-serif;
        margin: 2rem;
        background: #f6f7fb;
        color: #1f2a44;
      }
      header {
        margin-bottom: 1.5rem;
      }
      h1 {
        margin: 0 0 0.5rem 0;
      }
      .card {
        background: #fff;
        border-radius: 12px;
        padding: 1.5rem;
        box-shadow: 0 8px 20px rgba(31, 42, 68, 0.08);
        margin-bottom: 1.5rem;
      }
      label {
        font-weight: 600;
        display: block;
        margin-bottom: 0.25rem;
      }
      input, button {
        width: 100%;
        padding: 0.6rem 0.8rem;
        border-radius: 8px;
        border: 1px solid #d5d9e4;
        margin-bottom: 1rem;
      }
      button {
        background: #2d6cdf;
        color: #fff;
        font-weight: 600;
        border: none;
        cursor: pointer;
      }
      button:hover {
        background: #2357b6;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        text-align: left;
        padding: 0.75rem 0.5rem;
        border-bottom: 1px solid #eef0f5;
      }
      .badge {
        display: inline-block;
        padding: 0.2rem 0.5rem;
        border-radius: 999px;
        font-size: 0.8rem;
        font-weight: 600;
      }
      .badge.on-time { background: #e1f7e8; color: #1c7a3d; }
      .badge.late { background: #ffe4e4; color: #a40000; }
      .badge.early { background: #e3f0ff; color: #1d4f91; }
      .muted {
        color: #5f6b86;
      }
    </style>
  </head>
  <body>
    <header>
      <h1>Ruban Dashboard</h1>
      <p class="muted">Planifiez votre itinéraire et visualisez les retards en temps réel.</p>
    </header>
    <section class="card">
      <form id="plan-form">
        <label for="from-stop">Arrêt de départ</label>
        <input id="from-stop" list="stops-list" placeholder="Tapez le nom de l'arrêt..." required />
        <label for="to-stop">Arrêt d'arrivée</label>
        <input id="to-stop" list="stops-list" placeholder="Tapez le nom de l'arrêt..." required />
        <datalist id="stops-list"></datalist>
        <button type="submit">Calculer l'itinéraire</button>
      </form>
    </section>
    <section class="card">
      <h2>Prochains départs</h2>
      <table>
        <thead>
          <tr>
            <th>Ligne</th>
            <th>Destination</th>
            <th>Départ prévu</th>
            <th>Départ estimé</th>
            <th>Statut</th>
          </tr>
        </thead>
        <tbody id="results-body">
          <tr>
            <td colspan="5" class="muted">Sélectionnez un itinéraire pour afficher les départs.</td>
          </tr>
        </tbody>
      </table>
    </section>
    <script>
      async function loadStops() {
        const response = await fetch("/api/stops");
        const stops = await response.json();
        const dataList = document.getElementById("stops-list");
        stops.forEach((stop) => {
          const option = document.createElement("option");
          option.value = stop.stop_name;
          option.label = stop.lines ? `Lignes: ${stop.lines}` : "";
          dataList.appendChild(option);
        });
      }

      function formatStatus(delaySeconds) {
        if (delaySeconds === null || delaySeconds === undefined) {
          return '<span class="badge">Inconnu</span>';
        }
        if (delaySeconds > 30) {
          return `<span class="badge late">+${Math.round(delaySeconds / 60)} min</span>`;
        }
        if (delaySeconds < -30) {
          return `<span class="badge early">${Math.round(delaySeconds / 60)} min</span>`;
        }
        return '<span class="badge on-time">À l\\'heure</span>';
      }

      function formatTime(value) {
        return value ?? "—";
      }

      document.getElementById("plan-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const fromStop = document.getElementById("from-stop").value.trim();
        const toStop = document.getElementById("to-stop").value.trim();
        const response = await fetch(`/api/plan?from_stop=${encodeURIComponent(fromStop)}&to_stop=${encodeURIComponent(toStop)}`);
        const data = await response.json();
        const tbody = document.getElementById("results-body");
        tbody.innerHTML = "";
        if (data.trips.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" class="muted">Aucun départ trouvé pour ce trajet.</td></tr>';
          return;
        }
        data.trips.forEach((trip) => {
          const row = document.createElement("tr");
          row.innerHTML = `
            <td>${trip.route}</td>
            <td>${trip.destination}</td>
            <td>${formatTime(trip.scheduled_departure)}</td>
            <td>${formatTime(trip.expected_departure)}</td>
            <td>${formatStatus(trip.delay_seconds)}</td>
          `;
          tbody.appendChild(row);
        });
      });

      loadStops();
    </script>
  </body>
</html>
"""

GTFS_ZIP_PATH = None
GTFS_RT_URL = None


@lru_cache(maxsize=1)
def get_gtfs_data():
    if not GTFS_ZIP_PATH:
        raise ValueError("GTFS_ZIP_PATH non configuré.")
    return core.load_gtfs_data(GTFS_ZIP_PATH)


def build_delay_map(feed, now_datetime, gtfs_data):
    delay_map = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if not vehicle.trip.HasField("trip_id"):
            continue
        trip_id = vehicle.trip.trip_id
        seq = vehicle.current_stop_sequence if vehicle.HasField("current_stop_sequence") else None
        timestamp = datetime.fromtimestamp(vehicle.timestamp) if vehicle.HasField("timestamp") else now_datetime
        if seq is None:
            continue
        delay_map[trip_id] = core.estimate_delay_from_position(trip_id, seq, timestamp, now_datetime, gtfs_data)
    return delay_map


def build_trip_options(gtfs_data, from_stop_ids, to_stop_ids, now_datetime, delay_map, limit=5):
    stop_times = gtfs_data["stop_times"]
    trips = gtfs_data["trips"]
    routes = gtfs_data["routes"]

    from_rows = stop_times[stop_times["stop_id"].isin(from_stop_ids)]
    to_rows = stop_times[stop_times["stop_id"].isin(to_stop_ids)]
    if from_rows.empty or to_rows.empty:
        return []

    merged = from_rows.merge(
        to_rows,
        on="trip_id",
        suffixes=("_from", "_to"),
    )
    merged = merged[merged["stop_sequence_from"] < merged["stop_sequence_to"]]
    if merged.empty:
        return []

    today_base = datetime.combine(now_datetime.date(), datetime.min.time())
    options = []
    for _, row in merged.iterrows():
        dep_time = core.parse_gtfs_time(row["departure_time_from"], today_base)
        if dep_time is None:
            continue
        if dep_time < now_datetime - timedelta(minutes=2):
            continue
        trip_id = row["trip_id"]
        trip_row = trips[trips["trip_id"] == trip_id]
        if trip_row.empty:
            continue
        trip_info = trip_row.iloc[0]
        route_row = routes[routes["route_id"] == trip_info["route_id"]]
        route_name = route_row["route_short_name"].iloc[0] if not route_row.empty else trip_info["route_id"]
        destination = trip_info.get("trip_headsign", "?")
        delay_seconds = delay_map.get(trip_id)
        expected_departure = (
            (dep_time + timedelta(seconds=delay_seconds)).strftime("%H:%M:%S")
            if delay_seconds is not None
            else None
        )
        options.append(
            {
                "route": route_name,
                "destination": destination,
                "scheduled_departure": dep_time.strftime("%H:%M:%S"),
                "expected_departure": expected_departure,
                "delay_seconds": delay_seconds,
            }
        )

    options.sort(key=lambda item: item["scheduled_departure"])
    return options[:limit]


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/stops")
def stops():
    gtfs_data = get_gtfs_data()
    stops_df = gtfs_data["stops"]
    stop_times = gtfs_data["stop_times"]
    trips = gtfs_data["trips"]
    routes = gtfs_data["routes"]

    merged = stop_times.merge(trips[["trip_id", "route_id"]], on="trip_id", how="left")
    merged = merged.merge(routes[["route_id", "route_short_name"]], on="route_id", how="left")
    merged = merged.merge(stops_df[["stop_id", "stop_name"]], on="stop_id", how="left")

    line_map = (
        merged.groupby("stop_name")["route_short_name"]
        .apply(lambda series: ", ".join(sorted({str(x) for x in series.dropna() if x})))
        .to_dict()
    )

    unique_names = sorted(stops_df["stop_name"].dropna().unique())
    data = [
        {"stop_name": name, "lines": line_map.get(name, "")}
        for name in unique_names
    ]
    return jsonify(data)


@app.route("/api/plan")
def plan():
    from_stop = request.args.get("from_stop")
    to_stop = request.args.get("to_stop")
    if not from_stop or not to_stop:
        return jsonify({"trips": []})

    now_datetime = datetime.now()
    gtfs_data = get_gtfs_data()
    stops_df = gtfs_data["stops"]
    from_stop_ids = stops_df[stops_df["stop_name"] == from_stop]["stop_id"].tolist()
    to_stop_ids = stops_df[stops_df["stop_name"] == to_stop]["stop_id"].tolist()
    if not from_stop_ids or not to_stop_ids:
        return jsonify({"trips": []})
    feed = core.fetch_gtfs_rt(GTFS_RT_URL)
    delay_map = build_delay_map(feed, now_datetime, gtfs_data)
    trips = build_trip_options(gtfs_data, from_stop_ids, to_stop_ids, now_datetime, delay_map)
    return jsonify({"trips": trips, "generated_at": now_datetime.isoformat()})


def main():
    parser = argparse.ArgumentParser(description="Dashboard Ruban - itinéraires temps réel")
    parser.add_argument("--zip-path", required=True, help="Chemin vers le fichier GTFS.zip")
    parser.add_argument("--rt-url", required=True, help="URL du flux GTFS-Realtime")
    parser.add_argument("--host", default="0.0.0.0", help="Adresse d'écoute")
    parser.add_argument("--port", type=int, default=8000, help="Port d'écoute")
    args = parser.parse_args()

    global GTFS_ZIP_PATH, GTFS_RT_URL
    GTFS_ZIP_PATH = args.zip_path
    GTFS_RT_URL = args.rt_url
    get_gtfs_data()

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
