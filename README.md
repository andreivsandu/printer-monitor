# Printer Monitor

This project generates and serves a live office printer dashboard. It polls printers over SNMP, rebuilds `runtime/printer_dashboard.html`, and serves the dashboard over HTTP.

## Project files

The main project files are:

- `generate_dashboard.py`
- `app.py`
- `printers.csv` as committed printer inventory/config input
- `runtime/` for generated runtime artifacts such as `alert_state.json` and `printer_dashboard.html`

## Local development

For local development, use only the base Compose file so Docker does not require the external proxy network.

Set this in `.env`:

```env
COMPOSE_FILE=docker-compose.yml
```

Then start the app with:

```bash
docker compose up --build
```

In this mode:

- The app is available directly on `http://localhost:8000`
- If you are running locally via OrbStack, the app is also available by default at `http://printer-monitor.orb.local/` and `https://printer-monitor.orb.local/` through OrbStack's built-in reverse proxy
- The project root is bind-mounted into `/app` for live code edits
- The container serves the dashboard from `/`
- Generated artifacts are written under `/app/runtime`
- Dashboard and container log timestamps default to `America/Toronto`
- The dashboard is refreshed every 15 minutes by default

## Local dev un-dockerized

Initially:

```
pyenv virtualenv flask-apps # if not created already
pyenv activate flask-apps
pip install -r requirements.txt
```

After:
```
pyenv activate flask-apps
python app.py
```

## Legacy local script

`start_server.command` is now mostly optional, because the Dockerized web app already serves the dashboard and handles periodic refreshes.

It can still be useful if you want a simple non-Docker local workflow on a Mac, or if you want to run the generator and static file server directly from the project folder.

When you run `start_server.command`:

- It starts Python's built-in static web server on port `8000`
- It serves `runtime/printer_dashboard.html` directly
- It runs `generate_dashboard.py` in a loop every 15 minutes
- It does not run the Flask app, Docker healthcheck, or reverse-proxy-aware deployment setup

## Production deployment

For production, use both Compose files so the Caddy labels and external proxy network are enabled:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build
```

In this mode:

- The default public host is `printer-monitor.goldwaterdroit.com`
- Caddy proxies traffic to the container on internal port `8000`
- The source bind mount is removed
- The container runs Gunicorn
- `./printers.csv` on the host is bind-mounted read-only to `/app/printers.csv`, so you can update printer inventory data without rebuilding the image
- `./runtime` on the host is bind-mounted to `/app/runtime` so generated dashboard output and alert state persist outside the container

## Why you see the `werkzeug` warning
Because your **base** [docker-compose.yml](docker-compose.yml) runs:

```yaml
command: python app.py
```

and [app.py](app.py) contains:

```python
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
```

That starts Flask’s built-in development server, which is powered by `werkzeug`, and it logs:

- **"This is a development server. Do not use it in a production deployment."**

That is expected for the **dev/local** Compose path.

Your **production override** uses:

```yaml
command: gunicorn --bind 0.0.0.0:8000 app:app
```

So if you run with both Compose files, you should **not** get that development-server warning.

## Environment variables

Optional environment variables include:

- `PRINTER_MONITOR_REFRESH_INTERVAL_SECONDS`
- `PRINTER_MONITOR_TIMEZONE`
- `PRINTER_MONITOR_SEND_EMAILS`
- `PRINTER_MONITOR_SMTP_SERVER`
- `PRINTER_MONITOR_SMTP_PORT`
- `PRINTER_MONITOR_SMTP_EHLO_HOSTNAME`
- `PRINTER_MONITOR_SENDER_EMAIL`
- `PRINTER_MONITOR_RECIPIENT_EMAILS`
- `PRINTER_MONITOR_PORT`
- `PRINTER_MONITOR_SERVER_NAME`
- `FRONTEND_PROXY_NETWORK`
- `TZ`

## Notes

- The container installs the `snmp` package because the dashboard generator relies on `snmpget` and `snmpwalk`.
- The production override expects an existing external Docker network named `frontend-proxy` unless you set `FRONTEND_PROXY_NETWORK` differently.
- `printers.csv` is committed source data and is intentionally provided from the host in production instead of being baked into the image.
- Generated files are intentionally not committed; the app creates `runtime/alert_state.json` and `runtime/printer_dashboard.html` automatically.
- A host directory bind mount is used for runtime state because binding individual files that do not yet exist on the host is fragile; if you want to be explicit on first deploy, run `mkdir -p runtime` before starting the production stack.
- `PRINTER_MONITOR_SMTP_EHLO_HOSTNAME` should be set to a real SMTP HELO/EHLO identity, ideally the fully qualified domain name of the sending host when using Google Workspace SMTP relay.
- `PRINTER_MONITOR_TIMEZONE` controls application display timestamps and defaults to `America/Toronto`; invalid values fall back to that default.
- `TZ` controls the container process timezone and therefore log timestamps; by default it is also set to `America/Toronto` so the dashboard and logs stay aligned.
