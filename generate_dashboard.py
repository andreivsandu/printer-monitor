#!/usr/bin/python3
import os
import socket
import ssl
import csv
import subprocess
import json
import logging
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
import smtplib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# =========================
# CONFIGURATION
# =========================
BASE_PATH = os.environ.get("PRINTER_MONITOR_BASE_PATH", os.path.dirname(os.path.abspath(__file__)))
CSV_FILE = os.environ.get("PRINTER_MONITOR_CSV_FILE", f"{BASE_PATH}/printers.csv")
RUNTIME_DIR = os.environ.get("PRINTER_MONITOR_RUNTIME_DIR", f"{BASE_PATH}/runtime")
HTML_FILE = os.environ.get("PRINTER_MONITOR_HTML_FILE", f"{RUNTIME_DIR}/printer_dashboard.html")
STATE_FILE = os.environ.get("PRINTER_MONITOR_STATE_FILE", f"{RUNTIME_DIR}/alert_state.json")
INVENTORY_FILE = os.environ.get("PRINTER_MONITOR_INVENTORY_FILE", f"{RUNTIME_DIR}/inventory.json")
NEEDING_STATE_FILE = os.environ.get("PRINTER_MONITOR_NEEDING_STATE_FILE", f"{RUNTIME_DIR}/needing_state.json")
TONER_HISTORY_FILE = os.environ.get("PRINTER_MONITOR_TONER_HISTORY_FILE", f"{RUNTIME_DIR}/toner_history.json")

DEFAULT_TIMEZONE_NAME = "America/Toronto"
CONFIGURED_TIMEZONE_NAME = os.environ.get("PRINTER_MONITOR_TIMEZONE", os.environ.get("TZ", DEFAULT_TIMEZONE_NAME))
LOG_LEVEL = getattr(logging, os.environ.get("PRINTER_MONITOR_LOG_LEVEL", "INFO").upper(), logging.INFO)

if not logging.getLogger().handlers:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

logger = logging.getLogger("printer_monitor.dashboard")
logger.setLevel(LOG_LEVEL)

try:
    APP_TIMEZONE = ZoneInfo(CONFIGURED_TIMEZONE_NAME)
    APP_TIMEZONE_NAME = CONFIGURED_TIMEZONE_NAME
except ZoneInfoNotFoundError:
    APP_TIMEZONE = ZoneInfo(DEFAULT_TIMEZONE_NAME)
    APP_TIMEZONE_NAME = DEFAULT_TIMEZONE_NAME

if CONFIGURED_TIMEZONE_NAME != APP_TIMEZONE_NAME:
    logger.warning("Invalid timezone %s; falling back to %s", CONFIGURED_TIMEZONE_NAME, APP_TIMEZONE_NAME)

os.environ["TZ"] = APP_TIMEZONE_NAME
if hasattr(time, "tzset"):
    time.tzset()

logger.info("Using printer monitor timezone %s", APP_TIMEZONE_NAME)

# === EMAIL ALERTS CONFIG ===
SEND_EMAILS = os.environ.get("PRINTER_MONITOR_SEND_EMAILS", "true").lower() == "true"
SMTP_SERVER = os.environ.get("PRINTER_MONITOR_SMTP_SERVER", "smtp-relay.gmail.com")
SMTP_PORT = int(os.environ.get("PRINTER_MONITOR_SMTP_PORT", "25"))
SMTP_EHLO_HOSTNAME = os.environ.get("PRINTER_MONITOR_SMTP_EHLO_HOSTNAME", "").strip()
SMTP_LOG_PROTOCOL = os.environ.get("PRINTER_MONITOR_SMTP_LOG_PROTOCOL", "false").lower() == "true"
SMTP_STARTTLS = os.environ.get("PRINTER_MONITOR_SMTP_STARTTLS", "false").lower() == "true"
SMTP_REQUIRE_STARTTLS = os.environ.get("PRINTER_MONITOR_SMTP_REQUIRE_STARTTLS", "false").lower() == "true"
SENDER_EMAIL = os.environ.get("PRINTER_MONITOR_SENDER_EMAIL", "no-reply@goldwaterdroit.com")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("PRINTER_MONITOR_RECIPIENT_EMAILS", "as@goldwaterdroit.com").split(",") if e.strip()]

# === COMPANY POLICY ===
TONER_LOW_THRESHOLD = 5

# === USAGE TRACKING ===
HISTORY_MIN_INTERVAL_MINUTES = 30   # Store at most one data point per 30 min per printer
HISTORY_MAX_DAYS = 14               # Keep 14 days of history
USAGE_MIN_HOURS = 48                # Need at least 48 h of data before showing a label

# === SHOPPING LIST NICKNAMES ===
SUPPLY_MAPPING = {
    "HP M554": "HP M554 / M555 Series",
    "HP M555": "HP M554 / M555 Series",
    "HP LaserJet M402dn": "HP M402dn (Labelist)",
    "Canon C5170": "Canon C5170",
    "HP 2400": "HP 2400 (Exchequer)",
    "HP Color LaserJet 4525": "HP Color LaserJet 4525 (Dr. Zaius)",
    "HP Color LaserJet 4700": "HP Color LaserJet 4700 (RainbowAlliance)",
    "HP Color LaserJet CM4540f": "HP Color LaserJet CM4540f (Konstantieva)",
    "HP M880z": "HP M880z (Valkrye)",
    "imageFORCE C7165": "Canon C7165 (Orion)",
    "Xerox AltaLink C8145": "Xerox C8145 (Mercury)"
}


def format_smtp_response(response):
    if isinstance(response, bytes):
        return response.decode(errors='replace')
    return response


def sanitize_smtp_command(command_text):
    if command_text.upper().startswith("AUTH "):
        return "AUTH <redacted>"
    return command_text


class InstrumentedSMTP(smtplib.SMTP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_logging_enabled = False
        self.protocol_logger = None
        self.protocol_context = ""

    def configure_protocol_logging(self, enabled, protocol_logger, protocol_context):
        self.protocol_logging_enabled = enabled
        self.protocol_logger = protocol_logger
        self.protocol_context = protocol_context

    def connect(self, host='localhost', port=0, source_address=None):
        self._host = host
        return super().connect(host=host, port=port, source_address=source_address)

    def _log_protocol(self, direction, payload):
        if self.protocol_logging_enabled and self.protocol_logger:
            self.protocol_logger.debug("SMTP %s %s: %s", direction, self.protocol_context, payload)

    def putcmd(self, cmd, args=""):
        command_text = f"{cmd} {args}".strip()
        self._log_protocol("C", sanitize_smtp_command(command_text))
        return super().putcmd(cmd, args)

    def getreply(self):
        code, response = super().getreply()
        response_text = format_smtp_response(response)
        self._log_protocol("S", f"{code} {response_text}" if code != -1 else response_text)
        return code, response


def get_local_now():
    return datetime.now(APP_TIMEZONE)


def format_display_timestamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def get_smtp_local_hostname(sender_email):
    if SMTP_EHLO_HOSTNAME:
        return SMTP_EHLO_HOSTNAME
    system_fqdn = socket.getfqdn().strip()
    if system_fqdn and system_fqdn.lower() != "localhost" and "." in system_fqdn:
        return system_fqdn
    sender_domain = sender_email.rsplit("@", 1)[1].strip() if "@" in sender_email else ""
    if sender_domain:
        return sender_domain
    system_hostname = socket.gethostname().strip()
    if system_hostname and system_hostname.lower() != "localhost":
        return system_hostname
    return "localhost"


def ensure_parent_dir(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


# === EXACT PART NUMBERS CHEAT SHEET ===
PART_NUMBERS = {
    "HP M554 / M555 Series": {"Black": "W2120X", "Cyan": "W2121X", "Magenta": "W2123X", "Yellow": "W2122X"},
    "HP M402dn (Labelist)": {"Black": "CF226X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A"},
    "Canon C5170": {"Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow"},
    "HP 2400 (Exchequer)": {"Black": "Q6511X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A"},
    "HP Color LaserJet 4525 (Dr. Zaius)": {"Black": "CE260X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A"},
    "HP Color LaserJet 4700 (RainbowAlliance)": {"Black": "Q5950A", "Cyan": "Q5951A", "Magenta": "Q5953A", "Yellow": "Q5952A"},
    "HP Color LaserJet CM4540f (Konstantieva)": {"Black": "CE264X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A"},
    "HP Color LaserJet M551": {"Black": "CE400XC", "Cyan": "CE401AC", "Magenta": "CE403AC", "Yellow": "CE402AC"},
    "HP M553": {"Black": "CF360XC", "Cyan": "CF361XC", "Magenta": "CF363XC", "Yellow": "CF362XC"},
    "HP M651": {"Black": "CF330XC", "Cyan": "CF331AC", "Magenta": "CF333AC", "Yellow": "CF332AC"},
    "HP M880z (Valkrye)": {"Black": "CF300A", "Cyan": "CF301A", "Magenta": "CF303A", "Yellow": "CF302A"},
    "Canon C7165 (Orion)": {"Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow"},
    "Xerox C8145 (Mercury)": {"Black": "006R01746", "Cyan": "006R01747", "Magenta": "006R01748", "Yellow": "006R01749"},
}

# =========================
# OIDs & SNMP LOGIC
# =========================
DEFAULT_OIDS = {
    "device_status": "1.3.6.1.2.1.25.3.2.1.5.1",
    "printer_status": "1.3.6.1.2.1.25.3.5.1.1.1",
    "black":   "1.3.6.1.2.1.43.11.1.1.9.1.1",
    "cyan":    "1.3.6.1.2.1.43.11.1.1.9.1.2",
    "magenta": "1.3.6.1.2.1.43.11.1.1.9.1.3",
    "yellow":  "1.3.6.1.2.1.43.11.1.1.9.1.4",
}

MODEL_OIDS = {}


def snmp_get(ip, community, oid):
    value = run_snmp_command(ip, community, oid, "2c", "snmpget")
    if value is None:
        value = run_snmp_command(ip, community, oid, "1", "snmpget")
    return value


def run_snmp_command(ip, community, oid, version, command_type):
    cmd = [f"/usr/bin/{command_type}", f"-v{version}", "-c", community, "-t", "1", "-r", "0", ip, oid]
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5).decode(errors='ignore')
        if command_type == "snmpget":
            value = result.split(":")[-1].strip()
            if "(" in value and ")" in value:
                value = value.split("(")[-1].split(")")[0]
            return value
        return result
    except subprocess.TimeoutExpired:
        logger.debug("%s timed out for %s v%s oid %s", command_type, ip, version, oid)
    except subprocess.CalledProcessError:
        logger.debug("%s non-zero exit for %s v%s oid %s", command_type, ip, version, oid)
    except FileNotFoundError:
        logger.exception("SNMP command %s not found", command_type)
    except Exception:
        logger.exception("Unexpected error running %s for %s", command_type, ip)
    return None


def to_number(value):
    if value is None or value == "": return ""
    try: return int(value)
    except: return ""


def map_status(value):
    return {"2": "RUNNING", "3": "WARNING", "5": "DOWN"}.get(value, "OFFLINE")


def get_oid(model, key):
    return MODEL_OIDS.get(model, {}).get(key, DEFAULT_OIDS.get(key))


def get_supply_percent(ip, community, model, supply_key):
    level_oid = get_oid(model, supply_key)
    if not level_oid: return ""
    level = to_number(snmp_get(ip, community, level_oid))
    if level == "" or level in [-2, -3]: return level
    max_oid = level_oid.replace(".9.1.", ".8.1.")
    max_val = to_number(snmp_get(ip, community, max_oid))
    if isinstance(max_val, int) and max_val > 0:
        return int((level / max_val) * 100)
    return level


# =========================
# INVENTORY — NEEDING STATE
# =========================
def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json_file(path, data):
    ensure_parent_dir(path)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_inventory():
    if os.path.exists(INVENTORY_FILE):
        try:
            with open(INVENTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    inv = {model: {"Black": 0, "Cyan": 0, "Magenta": 0, "Yellow": 0} for model in PART_NUMBERS}
    save_json_file(INVENTORY_FILE, inv)
    return inv


def deduct_inventory_on_need_cleared(name, color, shopping_model, inventory, needing_state):
    """
    Called when a specific printer transitions from 'needing' a color to 'not needing' it.
    Deducts 1 from inventory for that model/color.
    This is far more reliable than watching aggregate shopping counts because:
    - Tied to a specific printer, not a group aggregate
    - Offline printers preserve their needing state, so a temporary dropout
      doesn't look like a replacement
    """
    if shopping_model in inventory and color in inventory[shopping_model]:
        current = inventory[shopping_model][color]
        inventory[shopping_model][color] = max(0, current - 1)
        logger.info(
            "📦 TONER REPLACED: %s %s on %s — inventory %s → %s",
            shopping_model, color, name, current, inventory[shopping_model][color],
        )


# =========================
# USAGE TRACKING
# =========================
def update_toner_history(history, name, now, b, c, m, y):
    """
    Append a toner reading for a printer if enough time has passed since the
    last stored point. Prune data older than HISTORY_MAX_DAYS.
    Only stores real integer values (not '-' or '?').
    """
    printer_history = history.get(name, [])

    # Respect minimum interval between stored points
    if printer_history:
        try:
            last_ts = datetime.fromisoformat(printer_history[-1]["ts"])
            if (now - last_ts).total_seconds() < HISTORY_MIN_INTERVAL_MINUTES * 60:
                return  # Too soon, skip
        except Exception:
            pass

    point = {"ts": now.isoformat()}
    if isinstance(b, int) and b >= 0: point["b"] = b
    if isinstance(c, int) and c >= 0: point["c"] = c
    if isinstance(m, int) and m >= 0: point["m"] = m
    if isinstance(y, int) and y >= 0: point["y"] = y

    if len(point) == 1:  # Only timestamp, no valid readings
        return

    printer_history.append(point)

    # Prune old data
    cutoff = (now - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    printer_history = [p for p in printer_history if p["ts"] >= cutoff]

    history[name] = printer_history


def calculate_usage_rate(printer_history, color_keys):
    """
    Returns total toner consumption in %/day across the given colors.
    Ignores periods where toner went UP (replacements).
    Returns None if insufficient data.
    """
    if len(printer_history) < 4:
        return None

    # Check we have enough time span
    try:
        first_ts = datetime.fromisoformat(printer_history[0]["ts"])
        last_ts = datetime.fromisoformat(printer_history[-1]["ts"])
        hours_span = (last_ts - first_ts).total_seconds() / 3600
    except Exception:
        return None

    if hours_span < USAGE_MIN_HOURS:
        return None

    total_decline = 0.0
    total_seconds = 0.0

    for i in range(1, len(printer_history)):
        prev = printer_history[i - 1]
        curr = printer_history[i]
        try:
            secs = (datetime.fromisoformat(curr["ts"]) - datetime.fromisoformat(prev["ts"])).total_seconds()
        except Exception:
            continue
        if secs <= 0:
            continue

        for key in color_keys:
            p_val = prev.get(key)
            c_val = curr.get(key)
            if isinstance(p_val, int) and isinstance(c_val, int) and p_val >= 0 and c_val >= 0:
                decline = p_val - c_val
                if decline > 0:  # Only count actual consumption, not replacements
                    total_decline += decline

        total_seconds += secs

    if total_seconds < USAGE_MIN_HOURS * 3600:
        return None

    return total_decline / (total_seconds / 86400)


def format_usage_label(rate):
    """Returns an HTML string for the usage column."""
    if rate is None:
        return "<span style='color:#7f8c8d;font-style:italic;'>Collecting…</span>"
    if rate >= 2.0:
        return f"<span style='color:#e74c3c;font-weight:bold;'>🔴 Heavy</span><br><small style='color:#7f8c8d'>{rate:.2f}%/day</small>"
    elif rate >= 0.4:
        return f"<span style='color:#f39c12;font-weight:bold;'>🟡 Medium</span><br><small style='color:#7f8c8d'>{rate:.2f}%/day</small>"
    else:
        return f"<span style='color:#2ecc71;font-weight:bold;'>🟢 Light</span><br><small style='color:#7f8c8d'>{rate:.2f}%/day</small>"


# =========================
# EMAIL & ALERTS
# =========================
def send_critical_email(printer_name, ip_address, error_text):
    if not SEND_EMAILS:
        logger.info("Email alerts disabled; skipping for %s", printer_name)
        return

    msg = EmailMessage()
    msg.set_content(
        f"CRITICAL PRINTER ALERT\n\nPrinter: {printer_name}\nIP Address: {ip_address}\n"
        f"Error: {error_text}\n\nDashboard: http://{ip_address}"
    )
    msg['Subject'] = f"🚨 Printer Alert: {printer_name} requires attention"
    msg['From'] = SENDER_EMAIL
    msg['To'] = ", ".join(RECIPIENT_EMAILS)
    smtp_local_hostname = get_smtp_local_hostname(SENDER_EMAIL)

    logger.info("Sending alert email for %s via %s:%s", printer_name, SMTP_SERVER, SMTP_PORT)
    try:
        with InstrumentedSMTP(local_hostname=smtp_local_hostname) as server:
            server.configure_protocol_logging(SMTP_LOG_PROTOCOL, logger, printer_name)
            server.connect(SMTP_SERVER, SMTP_PORT)
            server.ehlo()
            if SMTP_STARTTLS:
                if server.has_extn("starttls"):
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                elif SMTP_REQUIRE_STARTTLS:
                    raise smtplib.SMTPNotSupportedError("SMTP server does not advertise STARTTLS")
            server.send_message(msg, from_addr=SENDER_EMAIL, to_addrs=RECIPIENT_EMAILS)
        logger.info("Alert email sent for %s", printer_name)
    except Exception:
        logger.exception("Failed to send alert email for %s", printer_name)


def get_printer_alerts(ip, community):
    alert_oid = "1.3.6.1.2.1.43.18.1.1.8"
    raw_alerts = run_snmp_command(ip, community, alert_oid, "2c", "snmpwalk")
    if not raw_alerts:
        raw_alerts = run_snmp_command(ip, community, alert_oid, "1", "snmpwalk")
    if not raw_alerts:
        return {"text": "", "is_critical": False, "is_warning": False}

    ignore_list = ["ready", "sleep mode", "power save", "ok", "printing", "warming up", "sleep mode on"]
    critical_keywords = ["jam", "dead", "depleted", "exhausted", "service", "offline", "install", "failure", "41.", "49.", "50.", "59."]
    warning_keywords = ["low", "very low", "used supply", "open", "empty", "load", "paused", "cleaning", "tray", "error"]

    valid_alerts_found = []
    for line in raw_alerts.split("\n"):
        if "STRING:" in line:
            alert_text = line.split("STRING:")[-1].strip().strip('"')
            if alert_text and not any(i in alert_text.lower() for i in ignore_list):
                valid_alerts_found.append(alert_text)
    valid_alerts_found = list(dict.fromkeys(valid_alerts_found))

    is_critical = False
    is_warning = False
    for text in valid_alerts_found:
        safe = (text.lower()
                .replace("do not replace", "ignore_swap")
                .replace("services are unaffected", "ops fine")
                .replace("copy services", "copy ops")
                .replace("device services", "device ops")
                .replace("print and copy services", "print ops"))
        if any(kw in safe for kw in critical_keywords):
            is_critical = True
        elif any(kw in safe for kw in warning_keywords):
            is_warning = True

    return {"text": " | ".join(valid_alerts_found), "is_critical": is_critical, "is_warning": is_warning}


# =========================
# HTML GENERATOR
# =========================
def generate_html(printer_data, shopping_counts, inventory, timestamp):

    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Office Printer Dashboard</title>
    <style>
        :root {
            --bg-color: #f4f7f6; --text-color: #333; --table-bg: #fff;
            --table-border: #eee; --th-bg: #34495e; --th-text: #fff;
            --row-even: #f9fbfc; --row-hover: #f1f1f1; --link-color: #2980b9;
            --link-hover: #1abc9c; --shop-th: #2980b9; --bar-bg: #e0e0e0;
            --header-border: #3498db;
            --toner-black: #34495e; --toner-cyan: #00bcd4;
            --toner-magenta: #e91e63; --toner-yellow: #f1c40f;
        }
        [data-theme="dark"] {
            --bg-color: #121212; --text-color: #e0e0e0; --table-bg: #1e1e1e;
            --table-border: #333; --th-bg: #1f2b38; --th-text: #e0e0e0;
            --row-even: #252525; --row-hover: #2c2c2c; --link-color: #64b5f6;
            --link-hover: #81c784; --shop-th: #154360; --bar-bg: #444;
            --header-border: #64b5f6;
            --toner-black: #9aa7b1; --toner-cyan: #26c6da;
            --toner-magenta: #ec407a; --toner-yellow: #ffee58;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background-color: var(--bg-color); color: var(--text-color); transition: background 0.3s, color 0.3s; }
        .container { max-width: 1500px; margin: auto; }
        .header-bar { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid var(--header-border); padding-bottom: 10px; margin-bottom: 20px; }
        h1 { color: var(--header-border); margin: 0; }
        h2 { color: var(--link-color); margin-top: 40px; }
        .timestamp { color: #7f8c8d; font-size: 0.9em; font-style: italic; }
        table { width: 100%; border-collapse: collapse; background: var(--table-bg); border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid var(--table-border); }
        th { background-color: var(--th-bg); color: var(--th-text); text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.5px; cursor: pointer; user-select: none; transition: 0.2s; }
        th:hover { background-color: var(--link-hover); color: #fff; }
        tr:nth-child(even) { background-color: var(--row-even); }
        tr:hover { background-color: var(--row-hover); }
        a { color: var(--link-color); text-decoration: none; font-weight: bold; }
        a:hover { text-decoration: underline; color: var(--link-hover); }
        .offline { color: #e74c3c; font-weight: bold; }
        .online  { color: #2ecc71; font-weight: bold; }
        .shopping-table th { background-color: var(--shop-th); }
        .alert-critical { background-color: #ffcccc; color: black; font-weight: 500; padding: 4px 8px; border-radius: 4px; display: inline-block; }
        .alert-warning  { background-color: #ffe0b2; color: black; font-weight: 500; padding: 4px 8px; border-radius: 4px; display: inline-block; }
        .theme-toggle { background: var(--th-bg); color: var(--th-text); border: none; padding: 10px 15px; border-radius: 20px; cursor: pointer; font-weight: bold; transition: 0.3s; }
        .theme-toggle:hover { background: var(--link-hover); color: #fff; }
        .progress-container { width: 100%; min-width: 60px; background-color: var(--bar-bg); border-radius: 4px; position: relative; height: 22px; display: flex; align-items: center; overflow: hidden; border: 1px solid rgba(0,0,0,0.1); }
        .progress-bar { height: 100%; transition: width 0.5s ease; }
        .progress-text { position: absolute; width: 100%; text-align: center; font-size: 0.85em; font-weight: 700; color: #fff; text-shadow: 1px 1px 3px rgba(0,0,0,0.9), -1px -1px 3px rgba(0,0,0,0.9); pointer-events: none; }
    </style>
    <script>
        function resetName() {
            localStorage.removeItem('orderName');
            alert("Signature name has been reset!");
        }
        function sendOrderEmail(vendor, printerName, serial) {
            let userName = localStorage.getItem('orderName');
            if (!userName) {
                userName = prompt("First time setup: What is your name?");
                if (userName && userName.trim()) localStorage.setItem('orderName', userName.trim());
                else userName = "IT Support";
            }
            let subject = "Toner Order - " + serial;
            let printerDesc = printerName === 'Mercury' ? serial : "serial number " + serial;
            let body = `Dear sir/madam,\\n\\nOur printer (${printerDesc}) has run out of toner. We need a full set of colors and two blacks.\\n\\n--\\n${userName}\\nIT Support Team\\nT: (514) 861-4367\\n\\nGOLDWATER DROIT\\n3500 ouest boul. de Maisonneuve Blvd. West\\nSuite 2310, Office Tower 2\\nWestmount, QC H3Z 3C1`;
            let email = vendor === 'canon' ? 'canonsupplies@canada.canon.com' : 'cmorrison@printersplus.net';
            window.location.href = "mailto:" + email + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
        }
        if (localStorage.getItem('theme') === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
        function toggleDark() {
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
            document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
            localStorage.setItem('theme', isDark ? 'light' : 'dark');
        }
        function startAutoRefresh() {
            addSyncIndicator();
            setInterval(async () => {
                try {
                    const r = await fetch(window.location.pathname + "?t=" + Date.now(), { cache: 'no-store' });
                    if (!r.ok) return;
                    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
                    const nc = doc.getElementById('live-content');
                    if (nc) { document.getElementById('live-content').innerHTML = nc.innerHTML; addSyncIndicator(); }
                } catch(e) { console.error("Auto-refresh failed:", e); }
            }, 60000);
        }
        function addSyncIndicator() {
            const e = document.getElementById('frontend-sync'); if (e) e.remove();
            const d = document.createElement('div'); d.id = 'frontend-sync';
            d.style.cssText = 'font-size:0.9em;color:#2ecc71;font-weight:bold;margin-top:5px;';
            d.innerText = "⚡ Browser successfully checked server at: " + new Date().toLocaleTimeString();
            const ts = document.querySelector('.timestamp');
            if (ts) ts.parentNode.insertBefore(d, ts.nextSibling);
        }
        function sortTable(tableId, colIndex) {
            const table = document.getElementById(tableId);
            let switching = true, dir = "asc", switchcount = 0;
            while (switching) {
                switching = false;
                const rows = table.rows;
                for (let i = 1; i < rows.length - 1; i++) {
                    let x = (rows[i].cells[colIndex].innerText || rows[i].cells[colIndex].textContent).trim();
                    let y = (rows[i+1].cells[colIndex].innerText || rows[i+1].cells[colIndex].textContent).trim();
                    const nx = parseFloat(x.replace(/[^0-9.-]/g, '')), ny = parseFloat(y.replace(/[^0-9.-]/g, ''));
                    if (!isNaN(nx) && !isNaN(ny) && x.includes('%')) { x = nx; y = ny; }
                    else { x = x.toLowerCase(); y = y.toLowerCase(); }
                    if ((dir === "asc" && x > y) || (dir === "desc" && x < y)) {
                        rows[i].parentNode.insertBefore(rows[i+1], rows[i]);
                        switching = true; switchcount++; break;
                    }
                }
                if (!switching && switchcount === 0 && dir === "asc") { dir = "desc"; switching = true; }
            }
        }
        window.onload = startAutoRefresh;
    </script>
</head>
<body>
<div class="container">
    <div class="header-bar">
        <h1>🖨️ Office Printer Dashboard</h1>
        <div>
            <button class="theme-toggle" onclick="resetName()" style="margin-right:10px;">👤 Reset Signature Name</button>
            <button class="theme-toggle" onclick="toggleDark()">🌙 / ☀️ Toggle Theme</button>
        </div>
    </div>
"""

    html += f"""    <div id="live-content">
        <div class="timestamp">Last Updated: {timestamp}</div>

        <h2>🛒 Shopping List</h2>
        <table class="shopping-table" id="shopTable">
            <tr>
                <th onclick="sortTable('shopTable',0)">Model / Group ↕</th>
                <th onclick="sortTable('shopTable',1)">Black<br><small>(Need / Have)</small> ↕</th>
                <th onclick="sortTable('shopTable',2)">Cyan<br><small>(Need / Have)</small> ↕</th>
                <th onclick="sortTable('shopTable',3)">Magenta<br><small>(Need / Have)</small> ↕</th>
                <th onclick="sortTable('shopTable',4)">Yellow<br><small>(Need / Have)</small> ↕</th>
                <th>Parts to Order</th>
                <th>Order Here</th>
            </tr>
"""

    def get_inv_display(need, have):
        color = "#2ecc71" if have > need else "#f39c12" if have == need else "#e74c3c"
        return f"{need} / <strong><span style='color:{color}'>{have}</span></strong>"

    for model in sorted(shopping_counts.keys(), key=str.lower):
        counts = shopping_counts[model]
        inv_counts = inventory.get(model, {})
        model_parts = PART_NUMBERS.get(model, {})
        parts_to_order = [
            f"{item}: {model_parts.get(item, '?')}"
            for item in ["Black", "Cyan", "Magenta", "Yellow"]
            if counts[item] > 0
        ]
        parts_str = "<br>".join(parts_to_order) if parts_to_order else "None needed yet"

        if model == "Canon C5170":
            order_link = "<a href='#' onclick=\"sendOrderEmail('canon','Canon C5170','2XK0 2732 / 2XK0 3456');return false;\">📧 Email Canon</a>"
        elif "Orion" in model:
            order_link = "<a href='#' onclick=\"sendOrderEmail('canon','Orion','3TQ0 1854');return false;\">📧 Email Canon</a>"
        elif "Mercury" in model:
            order_link = "<a href='#' onclick=\"sendOrderEmail('printersplus','Mercury','Xerox AltaLink C8145');return false;\">📧 Email PrintersPlus</a>"
        else:
            order_link = "<a href='https://www.printersplus.net' target='_blank'>🌐 printersplus.net</a>"

        html += f"""            <tr>
                <td><strong>{model}</strong></td>
                <td>{get_inv_display(counts['Black'], inv_counts.get('Black', 0))}</td>
                <td>{get_inv_display(counts['Cyan'], inv_counts.get('Cyan', 0))}</td>
                <td>{get_inv_display(counts['Magenta'], inv_counts.get('Magenta', 0))}</td>
                <td>{get_inv_display(counts['Yellow'], inv_counts.get('Yellow', 0))}</td>
                <td>{parts_str}</td>
                <td>{order_link}</td>
            </tr>
"""

    html += """        </table>

        <h2>📊 Live Printer Status</h2>
        <table id="statusTable">
            <tr>
                <th onclick="sortTable('statusTable',0)">Name ↕</th>
                <th onclick="sortTable('statusTable',1)">IP Address ↕</th>
                <th onclick="sortTable('statusTable',2)">Model ↕</th>
                <th onclick="sortTable('statusTable',3)">Status ↕</th>
                <th onclick="sortTable('statusTable',4)">Current Alerts ↕</th>
                <th onclick="sortTable('statusTable',5)">Black ↕</th>
                <th onclick="sortTable('statusTable',6)">Cyan ↕</th>
                <th onclick="sortTable('statusTable',7)">Magenta ↕</th>
                <th onclick="sortTable('statusTable',8)">Yellow ↕</th>
                <th onclick="sortTable('statusTable',9)">Usage ↕</th>
            </tr>
"""

    def format_supply(val, color_type="black"):
        if val == "" or val is None: return "-"
        if val == "?": return "?"
        if isinstance(val, int):
            actual = max(0 if val == -2 else val, 0)
            colors = {"black": "var(--toner-black)", "cyan": "var(--toner-cyan)",
                      "magenta": "var(--toner-magenta)", "yellow": "var(--toner-yellow)"}
            bar = colors.get(color_type, "#95a5a6")
            return (f"<div class='progress-container'>"
                    f"<div class='progress-bar' style='width:{actual}%;background-color:{bar};'></div>"
                    f"<span class='progress-text'>{actual}%</span></div>")
        return val

    for p in printer_data:
        status_class = "offline" if p['online'] == "NO" or p['state'] == "DOWN" else "online"
        ad = p['alerts']
        alert_html = "-"
        alert_class = ""
        if ad['text']:
            alert_html = ad['text']
            alert_class = "class='alert-critical'" if ad['is_critical'] else ("class='alert-warning'" if ad['is_warning'] else "")

        html += f"""            <tr>
                <td><strong><a href="http://{p['ip']}" target="_blank">{p['name']}</a></strong></td>
                <td>{p['ip']}</td>
                <td>{p['model']}</td>
                <td class="{status_class}">{p['state']}</td>
                <td><span {alert_class}>{alert_html}</span></td>
                <td>{format_supply(p['b'], 'black')}</td>
                <td>{format_supply(p['c'], 'cyan')}</td>
                <td>{format_supply(p['m'], 'magenta')}</td>
                <td>{format_supply(p['y'], 'yellow')}</td>
                <td>{p['usage_html']}</td>
            </tr>
"""

    html += """        </table>
    </div>
</div>
</body>
</html>
"""
    ensure_parent_dir(HTML_FILE)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# =========================
# MAIN EXECUTION
# =========================
def main():
    printer_data = []
    ensure_parent_dir(STATE_FILE)
    ensure_parent_dir(HTML_FILE)
    logger.info("Starting dashboard generation")

    inventory    = load_inventory()
    needing_state = load_json_file(NEEDING_STATE_FILE, {})
    toner_history = load_json_file(TONER_HISTORY_FILE, {})
    alert_state   = load_json_file(STATE_FILE, {})

    new_needing_state = {}
    shopping_counts = {}
    current_broken_printers = []
    now = get_local_now()

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        logger.info("Loaded %s printer definitions", len(rows))

        for row in rows:
            ip, name, community = row["IP"], row["name"], row["community"]
            original_model = row.get("model", "Unknown Model")
            shopping_model = SUPPLY_MAPPING.get(original_model, original_model)

            if shopping_model not in shopping_counts:
                shopping_counts[shopping_model] = {"Black": 0, "Cyan": 0, "Magenta": 0, "Yellow": 0}

            dev_stat = snmp_get(ip, community, get_oid(original_model, "device_status"))
            online = "YES" if dev_stat else "NO"
            state  = map_status(dev_stat)

            if online == "NO":
                alerts_data = {"text": "Printer Offline or Unreachable", "is_critical": True, "is_warning": False}
                b = c = m = y = "-"
                # Preserve last known needing state for offline printers
                new_needing_state[name] = needing_state.get(name, {})
            else:
                alerts_data = get_printer_alerts(ip, community)
                model_parts = PART_NUMBERS.get(shopping_model, {})

                b = get_supply_percent(ip, community, original_model, "black")
                c = "-" if model_parts.get("Cyan")    == "N/A" else get_supply_percent(ip, community, original_model, "cyan")
                m = "-" if model_parts.get("Magenta") == "N/A" else get_supply_percent(ip, community, original_model, "magenta")
                y = "-" if model_parts.get("Yellow")  == "N/A" else get_supply_percent(ip, community, original_model, "yellow")

                # ── Inventory deduction via needing-state ──────────────────────
                # For each color with a real integer reading, check if the printer
                # just stopped needing a toner (low → OK). Offline printers are
                # excluded so a temporary dropout never triggers a false deduction.
                prev_needing = needing_state.get(name, {})
                curr_needing = {}

                for color, val in [("Black", b), ("Cyan", c), ("Magenta", m), ("Yellow", y)]:
                    if not isinstance(val, int):
                        # No real reading — carry forward whatever we knew before
                        curr_needing[color] = prev_needing.get(color, False)
                        continue

                    currently_needing = (val <= TONER_LOW_THRESHOLD or val == -2)
                    was_needing = prev_needing.get(color, currently_needing)  # default: no change on first run

                    if was_needing and not currently_needing:
                        deduct_inventory_on_need_cleared(name, color, shopping_model, inventory, needing_state)

                    curr_needing[color] = currently_needing

                new_needing_state[name] = curr_needing

                # ── Toner history for usage tracking ──────────────────────────
                update_toner_history(toner_history, name, now, b, c, m, y)

                # ── Failsafe alerts for 0% toners ─────────────────────────────
                failsafe_errors = []
                for color_name, val in [("Black Toner", b), ("Cyan Toner", c), ("Magenta Toner", m), ("Yellow Toner", y)]:
                    if isinstance(val, int) and (val == 0 or val == -2):
                        failsafe_errors.append(f"{color_name} is empty (0%)")
                if failsafe_errors:
                    alerts_data['is_warning'] = True
                    alerts_data['text'] = (alerts_data['text'] + " | " if alerts_data['text'] else "") + " | ".join(failsafe_errors)

            # ── Alert state / email ────────────────────────────────────────────
            if alerts_data['is_critical']:
                current_broken_printers.append(name)
                if name not in alert_state or alert_state[name] != alerts_data['text']:
                    if alerts_data['text'] != "Printer Offline or Unreachable":
                        send_critical_email(name, ip, alerts_data['text'])
                    alert_state[name] = alerts_data['text']

            # ── Shopping counts ────────────────────────────────────────────────
            for color, val in [("Black", b), ("Cyan", c), ("Magenta", m), ("Yellow", y)]:
                if isinstance(val, int) and (val <= TONER_LOW_THRESHOLD or val == -2):
                    shopping_counts[shopping_model][color] += 1

            # ── Usage rate ────────────────────────────────────────────────────
            model_parts = PART_NUMBERS.get(shopping_model, {})
            is_color = model_parts.get("Cyan") != "N/A"
            history_keys = ["b"] + (["c", "m", "y"] if is_color else [])
            rate = calculate_usage_rate(toner_history.get(name, []), history_keys)
            usage_html = format_usage_label(rate)

            printer_data.append({
                "name": name, "ip": ip, "model": original_model,
                "online": online, "state": state,
                "alerts": alerts_data,
                "b": b, "c": c, "m": m, "y": y,
                "usage_html": usage_html,
            })
            logger.debug("Polled %s: online=%s state=%s critical=%s", name, online, state, alerts_data['is_critical'])

    logger.info("Completed polling for %s printers", len(printer_data))

    # Clean up resolved alerts
    for p in [p for p in alert_state if p not in current_broken_printers]:
        del alert_state[p]

    # Persist state files
    save_json_file(STATE_FILE, alert_state)
    save_json_file(NEEDING_STATE_FILE, new_needing_state)
    save_json_file(TONER_HISTORY_FILE, toner_history)
    save_json_file(INVENTORY_FILE, inventory)

    printer_data.sort(key=lambda x: x['name'].lower())
    timestamp = format_display_timestamp(now)
    generate_html(printer_data, shopping_counts, inventory, timestamp)
    logger.info("Dashboard generated: %s printers, %s critical alerts", len(printer_data), len(current_broken_printers))


if __name__ == "__main__":
    main()