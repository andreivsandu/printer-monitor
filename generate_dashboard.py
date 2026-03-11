#!/usr/bin/python3
import os
import csv
import subprocess
import json
from datetime import datetime
from email.message import EmailMessage
import smtplib

# =========================
# CONFIGURATION
# =========================
BASE_PATH = os.environ.get("PRINTER_MONITOR_BASE_PATH", os.path.dirname(os.path.abspath(__file__)))
CSV_FILE = os.environ.get("PRINTER_MONITOR_CSV_FILE", f"{BASE_PATH}/printers.csv")
HTML_FILE = os.environ.get("PRINTER_MONITOR_HTML_FILE", f"{BASE_PATH}/printer_dashboard.html")
STATE_FILE = os.environ.get("PRINTER_MONITOR_STATE_FILE", f"{BASE_PATH}/alert_state.json") 

# === EMAIL ALERTS CONFIG ===
SEND_EMAILS = os.environ.get("PRINTER_MONITOR_SEND_EMAILS", "true").lower() == "true" 

SMTP_SERVER = os.environ.get("PRINTER_MONITOR_SMTP_SERVER", "smtp.goldwaterdube.com")  
SMTP_PORT = int(os.environ.get("PRINTER_MONITOR_SMTP_PORT", "25"))                 
SENDER_EMAIL = os.environ.get("PRINTER_MONITOR_SENDER_EMAIL", "no-reply@goldwaterdube.com")
RECIPIENT_EMAILS = [email.strip() for email in os.environ.get("PRINTER_MONITOR_RECIPIENT_EMAILS", "as@goldwaterdroit.com").split(",") if email.strip()]

# === COMPANY POLICY ===
TONER_LOW_THRESHOLD = 5     
WASTE_FULL_THRESHOLD = 90   

# === SHOPPING LIST NICKNAMES ===
SUPPLY_MAPPING = {
    "HP M554": "HP M554 / M555 Series",
    "HP M555": "HP M554 / M555 Series",
    "HP M402dn": "HP M402dn (Labelist)",
    "Canon C5860": "Canon C5860 (Galactica)",
    "HP 2400": "HP 2400 (Exchequer)",
    "HP Color LaserJet 4525": "HP Color LaserJet 4525 (Dr. Zaius)",
    "HP Color LaserJet 4700": "HP Color LaserJet 4700 (RainbowAlliance)",
    "HP Color LaserJet CM4540f": "HP Color LaserJet CM4540f (Konstantieva)",
    "HP M880z": "HP M880z (Valkrye)",
    "C7165": "C7165 (Orion)",
    "imageFORCE C7165": "imageFORCE C7165 (Orion)",
    "Xerox AltaLink C8145": "Xerox AltaLink C8145 (Mercury)"
}

FORCE_SHOPPING_LIST = [
    "HP 2400 (Exchequer)",
    "imageFORCE C7165 (Orion)",
    "C7165 (Orion)",
    "HP M402dn (Labelist)"
]

# === EXACT PART NUMBERS CHEAT SHEET ===
PART_NUMBERS = {
    "HP M554 / M555 Series": {
        "Black": "W2120X", "Cyan": "W2121X", "Magenta": "W2123X", "Yellow": "W2122X", "Fuser": "B5L35A", "Waste": "B5L37A"
    },
    "HP M402dn (Labelist)": {
        "Black": "CF226X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A", "Fuser": "RM2-5399", "Waste": "N/A"
    },
    "Canon C5860 (Galactica)": {
        "Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow", "Fuser": "Fuser", "Waste": "Waste"
    },
    "HP 2400 (Exchequer)": {
        "Black": "Q6511X", "Cyan": "N/A", "Magenta": "N/A", "Yellow": "N/A", "Fuser": "RM1-1535", "Waste": "N/A"
    },
    "HP Color LaserJet 4525 (Dr. Zaius)": {
        "Black": "CE260X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A", "Fuser": "CE246A", "Waste": "CE265A"
    },
    "HP Color LaserJet 4700 (RainbowAlliance)": {
        "Black": "Q5950A", "Cyan": "Q5951A", "Magenta": "Q5953A", "Yellow": "Q5952A", "Fuser": "Q7502A", "Waste": "N/A"
    },
    "HP Color LaserJet CM4540f (Konstantieva)": {
        "Black": "CE264X", "Cyan": "CE261A", "Magenta": "CE263A", "Yellow": "CE262A", "Fuser": "CE246A", "Waste": "CE265A"
    },
    "HP Color LaserJet M551": {
        "Black": "CE400XC", "Cyan": "CE401AC", "Magenta": "CE403AC", "Yellow": "CE402AC", "Fuser": "TBD", "Waste": "TBD"
    },
    "HP M553": {
        "Black": "CF360XC", "Cyan": "CF361XC", "Magenta": "CF363XC", "Yellow": "CF362XC", "Fuser": "TBD", "Waste": "TBD"
    },
    "HP M651": {
        "Black": "CF330XC", "Cyan": "CF331AC", "Magenta": "CF333AC", "Yellow": "CF332AC", "Fuser": "TBD", "Waste": "TBD"
    },
    "HP M880z (Valkrye)": {
        "Black": "CF300A", "Cyan": "CF301A", "Magenta": "CF303A", "Yellow": "CF302A", "Fuser": "C1N54A", "Waste": "C1N58A"
    },
    "imageFORCE C7165 (Orion)": {
        "Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow", "Fuser": "Fuser", "Waste": "Waste"
    },
    "Xerox AltaLink C8145 (Mercury)": {
        "Black": "006R01746", "Cyan": "006R01747", "Magenta": "006R01748", "Yellow": "006R01749", "Fuser": "TBD", "Waste": "008R13325"
    }
}

# =========================
# OIDs & SNMP LOGIC
# =========================
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

def snmp_get(ip, community, oid):
    value = run_snmp_command(ip, community, oid, "2c", "snmpget")
    if value is None:
        value = run_snmp_command(ip, community, oid, "1", "snmpget")
    return value

def run_snmp_command(ip, community, oid, version, command_type):
    try:
        cmd = [f"/usr/bin/{command_type}", f"-v{version}", "-c", community, ip, oid]
        result = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode(errors='ignore')
        
        if command_type == "snmpget":
            value = result.split(":")[-1].strip()
            if "(" in value and ")" in value:
                value = value.split("(")[-1].split(")")[0]
            return value
        return result
    except:
        return None

def to_number(value):
    if value is None or value == "": return ""
    try: return int(value)
    except: return ""

def format_waste(value):
    if value in [-2, -3]: return "OK"
    if value == -1: return "?"
    return value 

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

def send_critical_email(printer_name, ip_address, error_text):
    if not SEND_EMAILS:
        return
        
    msg = EmailMessage()
    msg.set_content(f"CRITICAL PRINTER ALERT\n\nPrinter: {printer_name}\nIP Address: {ip_address}\nError: {error_text}\n\nPlease check the dashboard for more details or click here to access the printer UI: http://{ip_address}")
    
    msg['Subject'] = f"🚨 Printer Alert: {printer_name} requires attention"
    msg['From'] = SENDER_EMAIL
    msg['To'] = ", ".join(RECIPIENT_EMAILS)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.send_message(msg)
        print(f"📧 Alert email sent for {printer_name}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def get_printer_alerts(ip, community):
    alert_oid = "1.3.6.1.2.1.43.18.1.1.8"
    
    raw_alerts = run_snmp_command(ip, community, alert_oid, "2c", "snmpwalk")
    if not raw_alerts:
        raw_alerts = run_snmp_command(ip, community, alert_oid, "1", "snmpwalk")
        
    if not raw_alerts:
        return {"text": "", "is_critical": False, "is_warning": False}

    ignore_list = ["ready", "sleep mode", "power save", "ok", "printing", "warming up", "sleep mode on"]
    
    critical_keywords = ["jam", "replace", "dead", "depleted", "exhausted", "error", "service", "offline", "install", "paused"] 
    warning_keywords = ["low", "very low", "used supply", "open", "empty", "load"] 

    valid_alerts_found = []

    for line in raw_alerts.split("\n"):
        if "STRING:" in line:
            alert_text = line.split("STRING:")[-1].strip().strip('"')
            
            is_ignored = False
            for ignored_item in ignore_list:
                if ignored_item in alert_text.lower():
                    is_ignored = True
                    break
            
            if alert_text and not is_ignored:
                valid_alerts_found.append(alert_text)
                
    valid_alerts_found = list(dict.fromkeys(valid_alerts_found))
    
    is_critical = False
    is_warning = False

    for text in valid_alerts_found:
        text_lower = text.lower()
        
        # --- NEW: FALSE POSITIVE SANITIZER ---
        # Hide phrases that contain trigger words but shouldn't trigger an alert
        safe_text = text_lower.replace("do not replace", "ignore_swap")
        safe_text = safe_text.replace("services are unaffected", "ops fine")
        safe_text = safe_text.replace("copy services", "copy ops")
        safe_text = safe_text.replace("device services", "device ops")
        safe_text = safe_text.replace("print and copy services", "print ops")
        
        is_current_critical = False
        for kw in critical_keywords:
            if kw in safe_text:
                is_current_critical = True
                is_critical = True
                break
        
        if not is_current_critical:
            for kw in warning_keywords:
                if kw in safe_text:
                    is_warning = True
                    break

    final_text = " | ".join(valid_alerts_found)
    return {"text": final_text, "is_critical": is_critical, "is_warning": is_warning}

# =========================
# HTML GENERATOR
# =========================
def generate_html(printer_data, shopping_counts, timestamp):
    
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="refresh" content="300">
        <title>Printer Dashboard</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 20px; background-color: #f4f7f6; color: #333; }
            .container { max-width: 1400px; margin: auto; }
            h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
            h2 { color: #2980b9; margin-top: 40px; }
            .timestamp { color: #7f8c8d; font-size: 0.9em; margin-bottom: 20px; font-style: italic; }
            
            table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background-color: #34495e; color: #fff; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.5px; }
            tr:nth-child(even) { background-color: #f9fbfc; }
            tr:hover { background-color: #f1f1f1; }
            
            a { color: #2980b9; text-decoration: none; }
            a:hover { text-decoration: underline; color: #1abc9c; }

            .offline { color: #e74c3c; font-weight: bold; }
            .online { color: #2ecc71; font-weight: bold; }
            .alert { color: #e74c3c; font-weight: bold; background: #ffeaea; padding: 2px 6px; border-radius: 4px; border: 1px solid #e74c3c; }
            .shopping-table th { background-color: #2980b9; }
            
            .alert-critical {
                background-color: #ffcccc; 
                color: black;
                font-weight: 500;
                padding: 4px 8px;
                border-radius: 4px;
            }
            .alert-warning {
                background-color: #ffe0b2; 
                color: black;
                font-weight: 500;
                padding: 4px 8px;
                border-radius: 4px;
            }
            .parts-list {
                color: #2980b9;
                font-weight: bold;
                font-size: 0.9em;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🖨️ Office Printer Dashboard</h1>
    """
    
    html += f'        <div class="timestamp">Last Updated: {timestamp}</div>\n'
    
    html += """
            <h2>🛒 Shopping List</h2>
            <table class="shopping-table">
                <tr>
                    <th>Model / Group</th>
                    <th>Black Qty</th>
                    <th>Cyan Qty</th>
                    <th>Magenta Qty</th>
                    <th>Yellow Qty</th>
                    <th>Fuser Qty</th>
                    <th>Waste Qty</th>
                    <th>Parts to Order</th>
                </tr>
    """

    sorted_models = sorted(shopping_counts.keys(), key=lambda x: x.lower())
    for model in sorted_models:
        counts = shopping_counts[model]
        if sum(counts.values()) > 0 or model in FORCE_SHOPPING_LIST:
            
            parts_to_order = []
            model_parts = PART_NUMBERS.get(model, {})
            
            for item in ["Black", "Cyan", "Magenta", "Yellow", "Fuser", "Waste"]:
                if counts[item] > 0:
                    part_no = model_parts.get(item, "?")
                    parts_to_order.append(f"{item}: {part_no}")
            
            parts_str = "<br>".join(parts_to_order) if parts_to_order else "None needed yet"
            
            html += f"""
                <tr>
                    <td><strong>{model}</strong></td>
                    <td>{counts['Black']}</td>
                    <td>{counts['Cyan']}</td>
                    <td>{counts['Magenta']}</td>
                    <td>{counts['Yellow']}</td>
                    <td>{counts['Fuser']}</td>
                    <td>{counts['Waste']}</td>
                    <td class="parts-list">{parts_str}</td>
                </tr>
            """

    html += """
            </table>
            
            <h2>📊 Live Printer Status</h2>
            <table>
                <tr>
                    <th>Name</th>
                    <th>IP Address</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th>Current Alerts</th>
                    <th>Black</th>
                    <th>Cyan</th>
                    <th>Magenta</th>
                    <th>Yellow</th>
                    <th>Fuser</th>
                    <th>Waste</th>
                </tr>
    """

    def format_supply(val, is_waste=False):
        if val == "" or val is None: return "-"
        if val in ["OK", "?"]: return val
        if isinstance(val, int):
            if is_waste:
                if val >= WASTE_FULL_THRESHOLD:
                    return f"<span class='alert'>{val}%</span>"
                else:
                    return "OK"
            else:
                if val <= TONER_LOW_THRESHOLD or val == -2:
                    display = "ERR" if val == -2 else f"{val}%"
                    return f"<span class='alert'>{display}</span>"
                return f"{val}%"
        return val

    for p in printer_data:
        status_class = "online" if p['online'] == "YES" else "offline"
        
        alerts_data = p['alerts']
        alerts_td_content = "-"
        alerts_td_class_attr = ""

        if alerts_data['text']:
            alerts_td_content = alerts_data['text']
            if alerts_data['is_critical']:
                alerts_td_class_attr = "class='alert-critical'"
            elif alerts_data['is_warning']:
                alerts_td_class_attr = "class='alert-warning'"
        
        html += f"""
                <tr>
                    <td><strong><a href="http://{p['ip']}" target="_blank">{p['name']}</a></strong></td>
                    <td>{p['ip']}</td>
                    <td>{p['model']}</td>
                    <td class="{status_class}">{p['state']}</td>
                    <td {alerts_td_class_attr}>{alerts_td_content}</td>
                    <td>{format_supply(p['b'])}</td>
                    <td>{format_supply(p['c'])}</td>
                    <td>{format_supply(p['m'])}</td>
                    <td>{format_supply(p['y'])}</td>
                    <td>{format_supply(p['fuser'])}</td>
                    <td>{format_supply(p['waste'], is_waste=True)}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    </body>
    </html>
    """
    
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

# =========================
# MAIN EXECUTION
# =========================
def main():
    printer_data = []
    
    shopping_counts = {}
    for forced_model in FORCE_SHOPPING_LIST:
        shopping_counts[forced_model] = {'Black': 0, 'Cyan': 0, 'Magenta': 0, 'Yellow': 0, 'Fuser': 0, 'Waste': 0}
    
    alert_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                alert_state = json.load(f)
        except:
            pass

    current_broken_printers = []

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ip, name, community = row["IP"], row["name"], row["community"]
            original_model = row.get("model", "Unknown Model")
            
            shopping_model_name = SUPPLY_MAPPING.get(original_model, original_model)

            if shopping_model_name not in shopping_counts:
                shopping_counts[shopping_model_name] = {'Black': 0, 'Cyan': 0, 'Magenta': 0, 'Yellow': 0, 'Fuser': 0, 'Waste': 0}

            dev_stat = snmp_get(ip, community, get_oid(original_model, "device_status"))
            
            online = "YES" if dev_stat else "NO"
            state = map_status(dev_stat)
            
            alerts_data = get_printer_alerts(ip, community)

            if alerts_data['is_critical']:
                current_broken_printers.append(name) 
                if name not in alert_state:          
                    send_critical_email(name, ip, alerts_data['text'])
                    alert_state[name] = True         

            b = get_supply_percent(ip, community, original_model, "black")
            c = get_supply_percent(ip, community, original_model, "cyan")
            m = get_supply_percent(ip, community, original_model, "magenta")
            y = get_supply_percent(ip, community, original_model, "yellow")
            fuser = get_supply_percent(ip, community, original_model, "fuser")
            waste_raw = get_supply_percent(ip, community, original_model, "waste")
            
            waste_display = format_waste(waste_raw)

            for color, val in [("Black", b), ("Cyan", c), ("Magenta", m), ("Yellow", y)]:
                if isinstance(val, int) and (val <= TONER_LOW_THRESHOLD or val == -2):
                    shopping_counts[shopping_model_name][color] += 1
            
            if isinstance(fuser, int) and (fuser <= TONER_LOW_THRESHOLD or fuser == 0 or fuser == -2):
                shopping_counts[shopping_model_name]['Fuser'] += 1

            if isinstance(waste_raw, int) and waste_raw >= 0 and waste_raw <= WASTE_FULL_THRESHOLD:
                shopping_counts[shopping_model_name]['Waste'] += 1

            printer_data.append({
                "name": name, "ip": ip, "model": original_model,
                "online": online, "state": state,
                "alerts": alerts_data, 
                "b": b, "c": c, "m": m, "y": y, "fuser": fuser, "waste": waste_display
            })

    printers_to_remove = [p for p in alert_state if p not in current_broken_printers]
    for p in printers_to_remove:
        del alert_state[p]
        
    with open(STATE_FILE, "w") as f:
        json.dump(alert_state, f)

    printer_data.sort(key=lambda x: x['name'].lower())

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    generate_html(printer_data, shopping_counts, timestamp)
    print(f"[{timestamp}] ✅ Dashboard generated successfully at: {HTML_FILE}")

if __name__ == "__main__":
    main()