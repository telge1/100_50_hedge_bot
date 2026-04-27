# Dashboard - Multi-Symbol Hedge Bot

Web-Dashboard zur Verwaltung aller Bot-Instanzen.

## 🚀 Installation

```bash
cd dashboard
pip install -r requirements.txt
```

## 🎯 Start

```bash
python app.py
```

Das Dashboard läuft dann auf: `http://0.0.0.0:5000`

**Wichtig**: Das Dashboard bindet auf `0.0.0.0`, damit es von anderen Geräten im Netzwerk erreichbar ist.

### Zugriff von anderen Geräten

1. Finde die IP-Adresse des Servers:
   ```bash
   hostname -I
   # oder
   ip addr show
   ```

2. Öffne im Browser auf deinem Laptop/Gerät:
   ```
   http://SERVER_IP:5000
   ```

## 🔐 Login

**Standard-Login** (beim ersten Start):
- Username: `telge`
- Password: `mwgitano40`

Die User-Datei wird automatisch erstellt: `dashboard_users.yaml`

## 📋 Features

- ✅ Login-System (Username/Password)
- ✅ Dark Mode Design
- ✅ Bot-Status anzeigen
- ✅ Zyklus-Status (Burn-Count, Rebuy-Status)
- ✅ Bot starten/stoppen/neustarten
- ✅ Schöne Buttons (Grün/Rot/Grau)
- ✅ Auto-Refresh alle 5 Sekunden

## 🔧 Systemd-Service (Optional)

Erstelle einen Systemd-Service für automatischen Start:

```ini
[Unit]
Description=Hedge Bot Dashboard
After=network.target

[Service]
Type=simple
User=telgenbuescher
WorkingDirectory=/home/telgenbuescher/projects/multisymbol_rebuy_hedge_bot/dashboard
ExecStart=/usr/bin/python3 /home/telgenbuescher/projects/multisymbol_rebuy_hedge_bot/dashboard/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

