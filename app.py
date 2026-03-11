import os
import threading
import time

from flask import Flask, jsonify, send_file

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


@app.route('/')
def index():
    ensure_refresh_thread_started()
    if not os.path.exists(HTML_FILE):
        message = last_refresh_error or 'Initial dashboard generation in progress'
        status_code = 503 if last_refresh_error else 202
        return f"Printer dashboard unavailable: {message}", status_code
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
