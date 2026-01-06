# 🚌 Ruban Monitor

**Ruban Monitor** est un outil Python de supervision en temps réel d’un réseau de transport public à partir de données **GTFS statiques** et **GTFS-Realtime**.

Il permet d’identifier rapidement :

* les véhicules en circulation,
* les retards et avances,
* les courses absentes,
* les doublons de véhicules,
* avec alertes visuelles et notifications Windows.

---

## 🚀 Fonctionnalités

* 📡 Lecture d’un flux **GTFS-Realtime (VehiclePositions)**
* 📁 Chargement d’un **GTFS statique (.zip)**
* ⏱ Détection des courses théoriquement en cours
* ❌ Détection des **courses absentes**
* ⚡ Détection des **doublons** (un véhicule sur plusieurs courses)
* ⏳ Calcul des **retards / avances**
* 🚏 Affichage du **prochain arrêt**
* 👥 Taux d’occupation (si fourni par le flux)
* 📊 Historique du retard moyen par ligne
* 🔔 Alertes :

  * bip console
  * notifications Windows
* 🎯 Mode *alert-only*
* 🚍 Suivi d’un véhicule spécifique

---

## 🛠️ Prérequis

* Python **3.9+**
* Un fichier **GTFS statique**
* Une URL **GTFS-Realtime** (Vehicle Positions)

> ⚠️ Les notifications ne fonctionnent que sous **Windows**.

---

## 📦 Installation

Cloner le dépôt :

```bash
git clone https://github.com/ton-utilisateur/ruban-monitor.git
cd ruban-monitor
```

Installer les dépendances :

```bash
pip install -r requirements.txt
```

---

## 📄 Dépendances

```
pandas
requests
gtfs-realtime-bindings
colorama
win10toast
```

---

## ▶️ Utilisation

### Lancement simple

```bash
python ruban_monitor.py gtfs.zip https://url_du_flux_gtfs_rt
```

### Options disponibles

| Option              | Description                               |
| ------------------- | ----------------------------------------- |
| `--interval 60`     | Intervalle de rafraîchissement (secondes) |
| `--alert-only`      | Affiche uniquement les alertes            |
| `--follow VEHICULE` | Suivre un véhicule précis                 |
| `--no-notif`        | Désactiver les notifications Windows      |
| `--no-beep`         | Désactiver le bip console                 |

### Exemple

```bash
python ruban_monitor.py gtfs.zip https://exemple.com/vehiclePositions.pb --interval 30 --alert-only
```

---

## 📂 Fichiers générés

| Fichier              | Description                      |
| -------------------- | -------------------------------- |
| `ruban_log.txt`      | Log des analyses                 |
| `ruban_history.json` | Historique des retards par ligne |

---

## 🧠 Logique de détection

* **Courses actives** :

  * Basées sur `calendar.txt` et `calendar_dates.txt`
  * Gestion des courses après minuit
* **Course absente** :

  * Course planifiée mais non observée en temps réel
* **Doublon** :

  * Un même véhicule affecté à plusieurs `trip_id`
* **Retard** :

  * Comparaison GTFS théorique vs position temps réel

---

## ⚠️ Limitations

* Fonctionne uniquement avec un flux **VehiclePositions**
* Dépend de la qualité des données GTFS
* Notifications Windows uniquement

---

## 🔮 Améliorations possibles

* Interface web / dashboard
* Carte temps réel
* Export CSV / JSON
* Support TripUpdates & Alerts
* Paramétrage des seuils d’alerte
* Mode service (daemon)

---

## 📜 Licence

Projet à usage **personnel / expérimental**.
Respecter les conditions d’utilisation des données GTFS fournies par l’exploitant.

---

⭐ N’hésite pas à ouvrir une *issue* ou proposer une *pull request* !
