# PHASE 6XT – DASHBOARD 8080 ROUTING VERIFICATION V1

**Date:** 2026-10-21  
**Mode:** Read-only (no restart, no config changes, no rsync)

## VERDICT

**`PORT8080_ROUTE_VERIFIED_READY_FOR_RELOAD`**

---

## 1. Prozesse und Listener

| Port | Listener (ss) | Prozess / Rolle |
|------|----------------|-----------------|
| **3000** | `0.0.0.0:3000` | **PID 1596085** — `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| **8080** | `*:8080` | **Apache2** (`/usr/sbin/apache2 -k start`, mehrere Worker-PIDs) |

CWD des Python-Prozesses:  
`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`

`PYTHONPATH` (aus `/proc/1596085/environ`):  
`spread_recovery_hedge_short_dev` → `…/dashboard` → `pools+ob+delta_bot` → `trading_research_platform`

---

## 2. Reverse-Proxy / VirtualHost (Dateisystem, lesbar)

### Apache (`Listen 8080` in `/etc/apache2/ports.conf`)

**`/etc/apache2/sites-enabled/dash.immotel.de.conf`:**

```apache
<VirtualHost 0.0.0.0:8080>
    ServerName dash.immotel.de
    ProxyPass / http://127.0.0.1:3000/
    ProxyPassReverse / http://127.0.0.1:3000/
    # WebSocket rewrite → ws://127.0.0.1:3000/...
</VirtualHost>
```

`apache2ctl -S`: auf Port **8080** u. a. `dash.immotel.de` (default) und `test.immotel.de` (wordpress.conf).

### Nginx (zusätzlich, nicht Port 8080 für dash)

**`/etc/nginx/sites-enabled/dash.immotel.de`:** `server_name dash.immotel.de` auf **80** und **443** → `proxy_pass http://127.0.0.1:3000;`

**Schlussfolgerung (konfigurationsbasiert):** Öffentliche URLs mit Host `dash.immotel.de` auf **:8080** werden von Apache an **127.0.0.1:3000** weitergeleitet.

---

## 3. DNS und Host-Zuordnung

| Check | Ergebnis |
|-------|----------|
| `getent hosts dash.immotel.de` | **80.151.239.228** |
| `curl ifconfig.me` auf diesem Host | **80.151.239.228** |

Damit löst `dash.immotel.de` auf **diesen** Server auf (keine abweichende Remote-IP in den Messungen).

---

## 4. HTTP-Nachweise (Research-App)

### Unauthentifiziert

| URL | HTTP | `Server`-Header (Body/Response) |
|-----|------|----------------------------------|
| `http://127.0.0.1:3000/live-charts/research` | **401** | `uvicorn` |
| `http://127.0.0.1:8080/live-charts/research` + `Host: dash.immotel.de` | **401** | `uvicorn` (via Apache-Proxy) |
| `http://dash.immotel.de:8080/live-charts/research` | **401** | `uvicorn` |
| `Host: test.immotel.de` auf `:8080` | **200** | WordPress-VirtualHost (anderer vHost) |

Identische 401-Antworten (SHA256 Body) für direkt :3000 und :8080 mit `dash.immotel.de` Host.

### Authentifiziert (Session-Cookie, read-only)

| URL | HTTP | HTML SHA256 |
|-----|------|-------------|
| `:3000/live-charts/research` | **200** | `d72f32f490cf8c32a7231670696d784dfa080e797a8bd07267877726a206e7f0` |
| `:8080/live-charts/research` + `Host: dash.immotel.de` | **200** | **identisch** |

Gemeinsame Kennzeichen im HTML (beide Dateien):

- `researchExactOriginPdWrap` — vorhanden  
- `researchExactOriginPoolDetail` — vorhanden (3×)  
- `researchExactOriginPd1Timeframe` — **nicht** vorhanden  
- `pd1_exact_origin_density_research_v1.js` — eingebunden  

### Static JS (öffentlich ohne Login)

| Quelle | HTTP | SHA256 | VERSION-String |
|--------|------|--------|----------------|
| Disk: `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | — | `93bde8839645c0e9d943df3b9db8c5aabb0d43a8bf52735d22083c451c9af727` | `phase6xt-density-pd-checkbox-v1` |
| `http://127.0.0.1:3000/static/js/research/...` | 200 | **gleich** | **gleich** |
| `http://127.0.0.1:8080/static/js/research/...` + Host dash | 200 | **gleich** | **gleich** |
| `http://dash.immotel.de:8080/static/js/research/...` | 200 | **gleich** | **gleich** |

---

## 5. Codepfad für `/live-charts/research`

| Schicht | Pfad |
|---------|------|
| Öffentlich | `http://dash.immotel.de:8080/...` |
| Apache | `ProxyPass` → `http://127.0.0.1:3000/` |
| Anwendung | `spread_recovery_hedge_short_dev/dashboard/app.py` (uvicorn) |
| Route | `research_charts/api.py` → Template `templates/research_charts.html` |
| Static | `dashboard/static/` (FastAPI `StaticFiles`) |

Es wurde **kein** zweiter paralleler Research-Server auf einem anderen Port für `dash.immotel.de:8080` nachgewiesen.

---

## 6. PD-Checkbox-Code auf :8080

**Bereits auslieferbar** über die öffentliche URL (ohne Dashboard-Restart in dieser Prüfung):

- HTML enthält `researchExactOriginPdWrap` / `researchExactOriginPoolDetail`
- JS-Asset enthält `phase6xt-density-pd-checkbox-v1`
- Extern und intern identische SHA256 für das JS

(Hinweis: In-Memory **Python**-API-Module können vom letzten Prozessstart abhängen; das betrifft nicht die statische/template-basierte Checkbox-UI, die hier nachweislich bereits über :8080 erreichbar ist.)

---

## 7. Welcher Prozess für einen Reload?

| Ziel | Prozess |
|------|---------|
| Backend/API, Sessions, Template-Rendering zur Laufzeit | **PID 1596085** — `python app.py` in `spread_recovery_hedge_short_dev/dashboard` |
| Apache-Proxy | Worker `apache2` — **nur** nötig bei Apache-Config-Änderungen (hier keine Änderung vorgenommen) |

Für reine Static/Template-Updates (PD-Checkbox-Phase): Nachweis zeigt **keinen** Reload zwingend für Sichtbarkeit des neuen JS/HTML über :8080; ein Reload von **1596085** wäre für **Python-Code/API**-Änderungen erforderlich.

---

## 8. Artefakte (lokal, Prüflauf)

- `/tmp/research3000.html`, `/tmp/research8080.html` — identische Research-HTML (auth)
- `/tmp/js3000.js`, `/tmp/js8080.js`, `/tmp/js_ext.js` — identische JS-SHA256

---

**STOP** — Routing verifiziert; Reload-Freigabe kann auf **einen** Backend-Prozess (1596085 / gleiches Startkommando) bezogen werden, nicht auf einen separaten „8080-Dashboard“-Stack.
