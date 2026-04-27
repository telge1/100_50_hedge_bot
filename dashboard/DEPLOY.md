# Dashboard Deployment (dash.immotel.de)

## Schnell-Deploy (nach Code-Änderungen)

```bash
cd ~/projects/burn_reentry_simple
bash scripts/deploy_dashboard.sh
```

Das Script macht:
1. `git pull` (optional)
2. Neustart des Dashboard-Services via systemctl
3. Test: Prüft ob `/api/profit-verlauf/closed-pnl` antwortet (HTTP 200 oder 401 = OK)

---

## Manueller Deploy

### 1. Code aktualisieren
```bash
cd ~/projects/burn_reentry_simple
git pull
```

### 2. Dashboard-Service neustarten
```bash
# Wenn systemd-Service installiert ist:
sudo systemctl restart hedgebot-dashboard

# Oder prüfen ob Service existiert:
sudo systemctl status hedgebot-dashboard
```

### 3. Lokal testen
```bash
# Endpoint testen (401 = Auth erforderlich, ist OK – 404 = Route fehlt)
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:3000/api/profit-verlauf/closed-pnl?account=main&limit=2"
# Erwartung: 401 (ohne Login) oder 200 (mit Session-Cookie)
```

---

## Nginx (dash.immotel.de)

Stelle sicher, dass `/api/` an den Dashboard-Port (3000) weitergeleitet wird:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    # ...
}
```

---

## Systemd-Service installieren (falls noch nicht)

```bash
sudo cp ~/projects/burn_reentry_simple/dashboard/dashboard.service /etc/systemd/system/hedgebot-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable hedgebot-dashboard
sudo systemctl start hedgebot-dashboard
```
