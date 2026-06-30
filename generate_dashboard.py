#!/usr/bin/python3
import os
import socket
import ssl
import csv
import subprocess
import json
import logging
import time
from datetime import datetime
from email.message import EmailMessage
import smtplib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from jinja2 import Environment, FileSystemLoader

# =========================
# CONFIGURATION
# =========================
BASE_PATH = os.environ.get("PRINTER_MONITOR_BASE_PATH", os.path.dirname(os.path.abspath(__file__)))
CSV_FILE = os.environ.get("PRINTER_MONITOR_CSV_FILE", f"{BASE_PATH}/printers.csv")
RUNTIME_DIR = os.environ.get("PRINTER_MONITOR_RUNTIME_DIR", f"{BASE_PATH}/runtime")
HTML_FILE = os.environ.get("PRINTER_MONITOR_HTML_FILE", f"{RUNTIME_DIR}/printer_dashboard.html")
STATE_FILE = os.environ.get("PRINTER_MONITOR_STATE_FILE", f"{RUNTIME_DIR}/alert_state.json") 
INVENTORY_FILE = os.environ.get("PRINTER_MONITOR_INVENTORY_FILE", f"{RUNTIME_DIR}/inventory.json")
TONER_STATE_FILE = os.environ.get("PRINTER_MONITOR_TONER_STATE_FILE", f"{RUNTIME_DIR}/toner_state.json")
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

os.environ["TZ"] = APP_TIMEZONE_NAME
if hasattr(time, "tzset"): time.tzset()

SEND_EMAILS = os.environ.get("PRINTER_MONITOR_SEND_EMAILS", "true").lower() == "true" 
SMTP_SERVER = os.environ.get("PRINTER_MONITOR_SMTP_SERVER", "smtp-relay.gmail.com")  
SMTP_PORT = int(os.environ.get("PRINTER_MONITOR_SMTP_PORT", "25"))                 
SMTP_EHLO_HOSTNAME = os.environ.get("PRINTER_MONITOR_SMTP_EHLO_HOSTNAME", "").strip()
SMTP_LOG_PROTOCOL = os.environ.get("PRINTER_MONITOR_SMTP_LOG_PROTOCOL", "false").lower() == "true"
SMTP_STARTTLS = os.environ.get("PRINTER_MONITOR_SMTP_STARTTLS", "false").lower() == "true"
SMTP_REQUIRE_STARTTLS = os.environ.get("PRINTER_MONITOR_SMTP_REQUIRE_STARTTLS", "false").lower() == "true"
SENDER_EMAIL = os.environ.get("PRINTER_MONITOR_SENDER_EMAIL", "no-reply@goldwaterdroit.com")
RECIPIENT_EMAILS = [email.strip() for email in os.environ.get("PRINTER_MONITOR_RECIPIENT_EMAILS", "as@goldwaterdroit.com").split(",") if email.strip()]

TONER_LOW_THRESHOLD = 5     
WASTE_FULL_THRESHOLD = 95   

SUPPLY_MAPPING = {
    "HP M554": "HP M554 / M555 Series", "HP M555": "HP M554 / M555 Series",
    "HP LaserJet M402dn": "HP M402dn (Labelist)", "Canon C5860": "Canon C5860 (Galactica)",
    "HP 2400": "HP 2400 (Exchequer)", "HP Color LaserJet 4525": "HP Color LaserJet 4525 (Dr. Zaius)",
    "HP Color LaserJet 4700": "HP Color LaserJet 4700 (RainbowAlliance)",
    "HP Color LaserJet CM4540f": "HP Color LaserJet CM4540f (Konstantieva)",
    "HP M880z": "HP M880z (Valkrye)", "imageFORCE C7165": "Canon C7165 (Orion)",
    "Xerox AltaLink C8145": "Xerox C8145 (Mercury)"
}

PART_NUMBERS = {
    "HP M554 / M555 Series": {"Black": "W2120X", "Cyan": "W2121X", "Magenta": "W2123X", "Yellow": "W2122X", "Fuser": "B5L35A", "Waste": "B5L37A"},
    "HP M402dn (Labelist)": {"Black": "CF226X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A", "Fuser": "RM2-5399", "Waste": "N/A"},
    "Canon C5860 (Galactica)": {"Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow", "Fuser": "Fuser", "Waste": "Waste"},
    "HP 2400 (Exchequer)": {"Black": "Q6511X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A", "Fuser": "RM1-1535", "Waste": "N/A"},
    "HP Color LaserJet 4525 (Dr. Zaius)": {"Black": "CE260X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A", "Fuser": "CE246A", "Waste": "CE265A"},
    "HP Color LaserJet 4700 (RainbowAlliance)": {"Black": "Q5950A", "Cyan": "Q5951A", "Magenta": "Q5953A", "Yellow": "Q5952A", "Fuser": "Q7502A", "Waste": "N/A"},
    "HP Color LaserJet CM4540f (Konstantieva)": {"Black": "CE264X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A", "Fuser": "CE246A", "Waste": "CE265A"},
    "HP Color LaserJet M551": {"Black": "CE400XC", "Cyan": "CE401AC", "Magenta": "CE403AC", "Yellow": "CE402AC", "Fuser": "TBD", "Waste": "TBD"},
    "HP M553": {"Black": "CF360XC", "Cyan": "CF361XC", "Magenta": "CF363XC", "Yellow": "CF362XC", "Fuser": "TBD", "Waste": "TBD"},
    "HP M651": {"Black": "CF330XC", "Cyan": "CF331AC", "Magenta": "CF333AC", "Yellow": "CF332AC", "Fuser": "TBD", "Waste": "TBD"},
    "HP M880z (Valkrye)": {"Black": "CF300A", "Cyan": "CF301A", "Magenta": "CF303A", "Yellow": "CF302A", "Fuser": "C1N54A", "Waste": "C1N58A"},
    "Canon C7165 (Orion)": {"Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow", "Fuser": "Fuser", "Waste": "Waste"},
    "Xerox C8145 (Mercury)": {"Black": "006R01746", "Cyan": "006R01747", "Magenta": "006R01748", "Yellow": "006R01749", "Fuser": "TBD", "Waste": "008R13325"}
}

DEFAULT_OIDS = {
    "device_status":  "1.3.6.1.2.1.25.3.2.1.5.1",
    "printer_status": "1.3.6.1.2.1.25.3.5.1.1.1",
    "black":          "1.3.6.1.2.1.43.11.1.1.9.1.1",
    "cyan":           "1.3.6.1.2.1.43.11.1.1.9.1.2",
    "magenta":        "1.3.6.1.2.1.43.11.1.1.9.1.3",
    "yellow":         "1.3.6.1.2.1.43.11.1.1.9.1.4",
    "fuser":          "1.3.6.1.2.1.43.11.1.1.9.1.5", 
    "waste":          "1.3.6.1.2.1.43.11.1.1.9.1.6",
}
MODEL_OIDS = {}

def get_local_now(): return datetime.now(APP_TIMEZONE)
def format_display_timestamp(value): return value.strftime("%Y-%m-%d %H:%M:%S %Z")
def ensure_parent_dir(path):
    directory = os.path.dirname(path)
    if directory: os.makedirs(directory, exist_ok=True)
def to_number(value):
    if value is None or value == "": return ""
    try: return int(value)
    except: return ""
def map_status(value): return {"2": "RUNNING", "3": "WARNING", "5": "DOWN"}.get(value, "OFFLINE")

def snmp_get(ip, community, oid):
    cmd = [f"/usr/bin/snmpget", f"-v2c", "-c", community, "-t", "1", "-r", "0", ip, oid]
    try:
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5).decode(errors='ignore')
        val = res.split(":")[-1].strip()
        return val.split("(")[-1].split(")")[0] if "(" in val else val
    except Exception:
        cmd[1] = "-v1"
        try:
            res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5).decode(errors='ignore')
            val = res.split(":")[-1].strip()
            return val.split("(")[-1].split(")")[0] if "(" in val else val
        except: return None

def snmp_walk(ip, community, oid):
    cmd = [f"/usr/bin/snmpwalk", f"-v2c", "-c", community, "-t", "1", "-r", "0", ip, oid]
    try: return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5).decode(errors='ignore')
    except Exception:
        cmd[1] = "-v1"
        try: return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5).decode(errors='ignore')
        except: return None

def get_supply_percent(ip, community, model, supply_key):
    level_oid = MODEL_OIDS.get(model, {}).get(supply_key, DEFAULT_OIDS.get(supply_key))
    if not level_oid: return ""
    level = to_number(snmp_get(ip, community, level_oid))
    if level == "" or level in [-2, -3]: return level
    max_val = to_number(snmp_get(ip, community, level_oid.replace(".9.1.", ".8.1.")))
    if isinstance(max_val, int) and max_val > 0: return int((level / max_val) * 100)
    return level

def send_critical_email(printer_name, ip_address, error_text):
    if not SEND_EMAILS: return
    msg = EmailMessage()
    msg.set_content(f"CRITICAL PRINTER ALERT\n\nPrinter: {printer_name}\nIP Address: {ip_address}\nError: {error_text}\n\nCheck dashboard: http://{ip_address}")
    msg['Subject'] = f"🚨 Printer Alert: {printer_name} requires attention"
    msg['From'] = SENDER_EMAIL
    msg['To'] = ", ".join(RECIPIENT_EMAILS)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            if SMTP_STARTTLS and server.has_extn("starttls"):
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.send_message(msg, from_addr=SENDER_EMAIL, to_addrs=RECIPIENT_EMAILS)
    except: pass

def get_printer_alerts(ip, community):
    raw_alerts = snmp_walk(ip, community, "1.3.6.1.2.1.43.18.1.1.8")
    if not raw_alerts: return {"text": "", "is_critical": False, "is_warning": False}
    ignore = ["ready", "sleep mode", "power save", "ok", "printing", "warming up", "sleep mode on"]
    critical = ["jam", "dead", "depleted", "exhausted", "service", "offline", "install", "failure", "41.", "49.", "50.", "59."] 
    warning = ["low", "very low", "used supply", "open", "empty", "load", "paused", "cleaning", "tray", "error"]

    found = []
    for line in raw_alerts.split("\n"):
        if "STRING:" in line:
            txt = line.split("STRING:")[-1].strip().strip('"')
            if txt and not any(ig in txt.lower() for ig in ignore): found.append(txt)
                
    found = list(dict.fromkeys(found))
    is_crit, is_warn = False, False
    for text in found:
        safe_text = text.lower().replace("do not replace", "ignore_swap").replace("services are unaffected", "ops fine")
        if any(kw in safe_text for kw in critical): is_crit = True
        elif any(kw in safe_text for kw in warning): is_warn = True
    return {"text": " | ".join(found), "is_critical": is_crit, "is_warning": is_warn}

def load_inventory():
    if os.path.exists(INVENTORY_FILE):
        try:
            with open(INVENTORY_FILE, "r") as f: return json.load(f)
        except Exception: pass
    inv = {model: {"Black": 0, "Cyan": 0, "Magenta": 0, "Yellow": 0, "Fuser": 0, "Waste": 0} for model in PART_NUMBERS}
    with open(INVENTORY_FILE, "w") as f: json.dump(inv, f, indent=4)
    return inv

def save_inventory(inv):
    with open(INVENTORY_FILE, "w") as f: json.dump(inv, f, indent=4)

def is_swap(prev_val, curr_val, is_waste=False):
    if curr_val == "-" or prev_val == "-": return False
    try:
        c = int(curr_val)
        p = int(prev_val) if prev_val not in ["REPLACE", "OK", "?"] else (99 if prev_val == "REPLACE" else 0)
    except: return False
    if c in [-2, -3] or p in [-2, -3]: return False
    return (p >= 80 and c <= 20) if is_waste else (p <= 15 and c >= 95)

def get_inv_display(need, have):
    color = "#2ecc71" if have > need else ("#f39c12" if have == need else "#e74c3c")
    return f"{need} / <strong><span style='color:{color}'>{have}</span></strong>"

def format_supply(val, is_waste=False, color_type="black"):
    if val == "" or val is None or val == "-": return "-"
    if val in ["OK", "?"]: return val
    if val == "REPLACE": return "<span class='offline'>REPLACE</span>"
    if isinstance(val, int):
        bar_color = {"black": "var(--toner-black)", "cyan": "var(--toner-cyan)", "magenta": "var(--toner-magenta)", "yellow": "var(--toner-yellow)"}.get(color_type, "var(--toner-fuser)")
        actual_val = 0 if val == -2 else val
        if is_waste:
            if actual_val >= WASTE_FULL_THRESHOLD: bar_color = "#e74c3c" 
            else: return "OK"
        width = actual_val if actual_val >= 0 else 0
        return f"<div class='progress-container'><div class='progress-bar' style='width: {width}%; background-color: {bar_color};'></div><span class='progress-text'>{actual_val}%</span></div>"
    return val

def generate_html(printer_data, shopping_counts, inventory, timestamp):
    # Prepare Shopping Rows
    shopping_rows = []
    for model in sorted(shopping_counts.keys(), key=lambda x: x.lower()):
        counts = shopping_counts[model]
        inv_counts = inventory.get(model, {})
        parts = [f"{i}: {PART_NUMBERS.get(model, {}).get(i, '?')}" for i in ["Black", "Cyan", "Magenta", "Yellow", "Fuser", "Waste"] if counts[i] > 0]
        
        olink = "<a href='https://www.printersplus.net' target='_blank'>🌐 printersplus.net</a>"
        if "Galactica" in model: olink = f"<a href='#' onclick=\"sendOrderEmail('canon', 'Galactica', '2XK0 2732'); return false;\">📧 Email Canon</a>"
        elif "Orion" in model: olink = f"<a href='#' onclick=\"sendOrderEmail('canon', 'Orion', '3TQ0 1854'); return false;\">📧 Email Canon</a>"
        elif "Mercury" in model: olink = f"<a href='#' onclick=\"sendOrderEmail('printersplus', 'Mercury', 'Xerox AltaLink C8145'); return false;\">📧 Email PrintersPlus</a>"

        shopping_rows.append({
            "model": model,
            "inv_black": get_inv_display(counts['Black'], inv_counts.get('Black', 0)),
            "inv_cyan": get_inv_display(counts['Cyan'], inv_counts.get('Cyan', 0)),
            "inv_magenta": get_inv_display(counts['Magenta'], inv_counts.get('Magenta', 0)),
            "inv_yellow": get_inv_display(counts['Yellow'], inv_counts.get('Yellow', 0)),
            "inv_fuser": get_inv_display(counts['Fuser'], inv_counts.get('Fuser', 0)),
            "inv_waste": get_inv_display(counts['Waste'], inv_counts.get('Waste', 0)),
            "parts_str": "<br>".join(parts) if parts else "None needed yet",
            "order_link": olink
        })

    # Prepare Printer Display Strings
    for p in printer_data:
        p['status_class'] = "offline" if p['online'] == "NO" or p['state'] == "DOWN" else "online"
        p['alert_class'] = "alert-critical" if p['alerts']['is_critical'] else ("alert-warning" if p['alerts']['is_warning'] else "")
        if not p['alerts']['text']: p['alerts']['text'] = "-"
        
        p['html_b'] = format_supply(p['b'], color_type='black')
        p['html_c'] = format_supply(p['c'], color_type='cyan')
        p['html_m'] = format_supply(p['m'], color_type='magenta')
        p['html_y'] = format_supply(p['y'], color_type='yellow')
        p['html_fuser'] = format_supply(p['fuser'], color_type='fuser')
        p['html_waste'] = format_supply(p['waste'], is_waste=True, color_type='waste')

    # Render Template using Jinja2
    env = Environment(loader=FileSystemLoader(BASE_PATH))
    template = env.get_template('dashboard_template.html')
    output_html = template.render(
        timestamp=timestamp, shopping_rows=shopping_rows,
        printer_data=printer_data
    )
    ensure_parent_dir(HTML_FILE)
    with open(HTML_FILE, "w", encoding="utf-8") as f: f.write(output_html)

# =========================
# MAIN EXECUTION
# =========================
def main():
    ensure_parent_dir(STATE_FILE)
    ensure_parent_dir(HTML_FILE)
    shopping_counts = {}
    inventory = load_inventory()
    
    toner_state = {}
    if os.path.exists(TONER_STATE_FILE):
        try:
            with open(TONER_STATE_FILE, "r") as f: toner_state = json.load(f)
        except Exception: pass
    new_toner_state = {}
    
    alert_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: alert_state = json.load(f)
        except Exception: pass

    current_broken = []
    printer_data = []

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        for row in rows:
            # Reverting back to original standard CSV headers
            ip = row.get("IP", "").strip()
            name = row.get("name", "").strip()
            community = row.get("community", "public").strip()
            orig_model = row.get("model", "Unknown Model").strip()
            
            if not ip or not name: continue

            shop_model = SUPPLY_MAPPING.get(orig_model, orig_model)
            if shop_model not in shopping_counts: shopping_counts[shop_model] = {'Black':0,'Cyan':0,'Magenta':0,'Yellow':0,'Fuser':0,'Waste':0}

            online = "YES" if snmp_get(ip, community, DEFAULT_OIDS["device_status"]) else "NO"
            state = map_status(snmp_get(ip, community, DEFAULT_OIDS["device_status"]))
            
            if online == "NO":
                alerts = {"text": "Printer Offline or Unreachable", "is_critical": True, "is_warning": False}
                b=c=m=y=fuser=w_raw=w_disp = "-"
            else:
                alerts = get_printer_alerts(ip, community)
                parts = PART_NUMBERS.get(shop_model, {})
                b = get_supply_percent(ip, community, orig_model, "black")
                c = "-" if parts.get("Cyan") == "N/A" else get_supply_percent(ip, community, orig_model, "cyan")
                m = "-" if parts.get("Magenta") == "N/A" else get_supply_percent(ip, community, orig_model, "magenta")
                y = "-" if parts.get("Yellow") == "N/A" else get_supply_percent(ip, community, orig_model, "yellow")
                fuser = get_supply_percent(ip, community, orig_model, "fuser")
                w_raw = "-" if parts.get("Waste") == "N/A" else get_supply_percent(ip, community, orig_model, "waste")
                
                if "Xerox" in orig_model or "AltaLink" in orig_model: w_raw = -3
                if any(kw in alerts['text'].lower() for kw in ["replace toner collection unit", "waste toner"]): w_raw = w_disp = "REPLACE"
                else: w_disp = w_raw if w_raw not in [-2,-3,-1] else ("OK" if w_raw in [-2,-3] else "?")

                curr_levels = {"Black": b, "Cyan": c, "Magenta": m, "Yellow": y, "Fuser": fuser, "Waste": w_raw}
                new_toner_state[name] = curr_levels
                
                for color_name, curr_val in curr_levels.items():
                    if is_swap(toner_state.get(name, {}).get(color_name, "-"), curr_val, is_waste=(color_name=="Waste")):
                        if shop_model in inventory: inventory[shop_model][color_name] = max(0, inventory[shop_model].get(color_name, 0) - 1)
            
            if online == "NO": new_toner_state[name] = toner_state.get(name, {})

            failsafe = [f"{col} is empty (0%)" for col, val in [("Black Toner", b), ("Cyan Toner", c), ("Magenta Toner", m), ("Yellow Toner", y), ("Fuser", fuser)] if isinstance(val, int) and val in [0, -2]]
            if failsafe:
                alerts['is_warning'] = True 
                alerts['text'] = alerts['text'] + " | " + " | ".join(failsafe) if alerts['text'] else " | ".join(failsafe)

            if alerts['is_critical']:
                current_broken.append(name) 
                if name not in alert_state or alert_state[name] != alerts['text']:
                    if alerts['text'] != "Printer Offline or Unreachable": send_critical_email(name, ip, alerts['text'])
                    alert_state[name] = alerts['text']

            for color, val in [("Black", b), ("Cyan", c), ("Magenta", m), ("Yellow", y)]:
                if isinstance(val, int) and (val <= TONER_LOW_THRESHOLD or val == -2): shopping_counts[shop_model][color] += 1
            if isinstance(fuser, int) and (fuser <= TONER_LOW_THRESHOLD or fuser in [0, -2]): shopping_counts[shop_model]['Fuser'] += 1
            if (isinstance(w_raw, int) and w_raw >= WASTE_FULL_THRESHOLD) or w_raw == "REPLACE": shopping_counts[shop_model]['Waste'] += 1

            printer_data.append({
                "name": name, "ip": ip, "model": orig_model, "online": online, "state": state, "alerts": alerts, 
                "b": b, "c": c, "m": m, "y": y, "fuser": fuser, "waste": w_disp
            })

    for p in [p for p in alert_state if p not in current_broken]: del alert_state[p]
    with open(STATE_FILE, "w") as f: json.dump(alert_state, f)
    save_inventory(inventory)
    with open(TONER_STATE_FILE, "w") as f: json.dump(new_toner_state, f)

    printer_data.sort(key=lambda x: x['name'].lower())
    generate_html(printer_data, shopping_counts, inventory, format_display_timestamp(get_local_now()))

if __name__ == "__main__": main()