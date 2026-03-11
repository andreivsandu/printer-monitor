import os
import threading
import time

from flask import Flask, Response, jsonify, send_file

from generate_dashboard import HTML_FILE, main as generate_dashboard

app = Flask(__name__)

REFRESH_INTERVAL_SECONDS = int(os.environ.get("PRINTER_MONITOR_REFRESH_INTERVAL_SECONDS", "900"))
last_refresh_error = None
refresh_lock = threading.Lock()
refresh_thread = None
refresh_thread_lock = threading.Lock()


def refresh_dashboard():
    global last_refresh_error
    with refresh_lock:
        print("[printer-monitor] Starting dashboard refresh...", flush=True)
        try:
            generate_dashboard()
            last_refresh_error = None
            print("[printer-monitor] Dashboard refresh completed.", flush=True)
        except Exception as exc:
            last_refresh_error = str(exc)
            print(f"[printer-monitor] Dashboard refresh failed: {exc}", flush=True)


def background_refresh_loop():
    while True:
        refresh_dashboard()
        time.sleep(REFRESH_INTERVAL_SECONDS)


def ensure_refresh_thread_started():
    global refresh_thread
    with refresh_thread_lock:
        if refresh_thread is None or not refresh_thread.is_alive():
            print("[printer-monitor] Starting background refresh thread.", flush=True)
            refresh_thread = threading.Thread(target=background_refresh_loop, daemon=True)
            refresh_thread.start()


def render_startup_page(message, status_code):
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <meta http-equiv=\"refresh\" content=\"5\">
    <title>Printer Monitor</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: #f4f7f6; color: #1f2933; margin: 0; }}
        .wrap {{ min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
        .card {{ max-width: 640px; width: 100%; background: #ffffff; border-radius: 16px; box-shadow: 0 18px 40px rgba(15, 23, 42, 0.12); padding: 32px; }}
        h1 {{ margin: 0 0 12px; font-size: 1.75rem; }}
        p {{ margin: 0 0 12px; line-height: 1.6; }}
        .muted {{ color: #52606d; }}
        .pill {{ display: inline-block; margin-top: 8px; padding: 8px 12px; border-radius: 999px; background: #e0f2fe; color: #075985; font-weight: 600; }}
        a {{ color: #0f6fff; text-decoration: none; }}
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"card\">
            <h1>Printer dashboard is warming up</h1>
            <p>{message}</p>
            <p class=\"muted\">The server is up, but the first SNMP polling cycle is still generating the dashboard. This page will refresh automatically every 5 seconds.</p>
            <div class=\"pill\">Status: starting</div>
            <p class=\"muted\"><a href=\"/health\">View health status</a></p>
        </div>
    </div>
</body>
</html>"""
    return Response(html, status=status_code, mimetype="text/html")


@app.route('/')
def index():
    ensure_refresh_thread_started()
    if not os.path.exists(HTML_FILE):
        message = last_refresh_error or 'Initial dashboard generation in progress'
        status_code = 503 if last_refresh_error else 202
        return render_startup_page(message, status_code)
    return send_file(HTML_FILE)


@app.route('/printer_dashboard.html')
def dashboard_html():
    return index()


@app.route('/health')
def health():
    ensure_refresh_thread_started()
    return jsonify({
        'status': 'degraded' if last_refresh_error else 'ok',
        'dashboard_exists': os.path.exists(HTML_FILE),
        'refresh_in_progress': refresh_lock.locked(),
        'last_refresh_error': last_refresh_error,
    })


ensure_refresh_thread_started()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
