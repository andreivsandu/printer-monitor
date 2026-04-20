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

# =========================
# CONFIGURATION
# =========================
BASE_PATH = os.environ.get("PRINTER_MONITOR_BASE_PATH", os.path.dirname(os.path.abspath(__file__)))
CSV_FILE = os.environ.get("PRINTER_MONITOR_CSV_FILE", f"{BASE_PATH}/printers.csv")
RUNTIME_DIR = os.environ.get("PRINTER_MONITOR_RUNTIME_DIR", f"{BASE_PATH}/runtime")
HTML_FILE = os.environ.get("PRINTER_MONITOR_HTML_FILE", f"{RUNTIME_DIR}/printer_dashboard.html")
STATE_FILE = os.environ.get("PRINTER_MONITOR_STATE_FILE", f"{RUNTIME_DIR}/alert_state.json") 
DEFAULT_TIMEZONE_NAME = "America/Toronto"
CONFIGURED_TIMEZONE_NAME = os.environ.get("PRINTER_MONITOR_TIMEZONE", os.environ.get("TZ", DEFAULT_TIMEZONE_NAME))
LOG_LEVEL = getattr(logging, os.environ.get("PRINTER_MONITOR_LOG_LEVEL", "INFO").upper(), logging.INFO)

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

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
RECIPIENT_EMAILS = [email.strip() for email in os.environ.get("PRINTER_MONITOR_RECIPIENT_EMAILS", "as@goldwaterdroit.com").split(",") if email.strip()]

# === COMPANY POLICY ===
TONER_LOW_THRESHOLD = 5     
WASTE_FULL_THRESHOLD = 95   

# === SHOPPING LIST NICKNAMES ===
# === SHOPPING LIST NICKNAMES ===
SUPPLY_MAPPING = {
    "HP M554": "HP M554 / M555 Series",
    "HP M555": "HP M554 / M555 Series",
    "HP LaserJet M402dn": "HP M402dn (Labelist)",
    "Canon C5860": "Canon C5860 (Galactica)",
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
        if code == -1:
            self._log_protocol("S", response_text)
        else:
            self._log_protocol("S", f"{code} {response_text}")
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
    "Canon C7165 (Orion)": {
        "Black": "Black", "Cyan": "Cyan", "Magenta": "Magenta", "Yellow": "Yellow", "Fuser": "Fuser", "Waste": "Waste"
    },
    "Xerox C8145 (Mercury)": {
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
        logger.debug("Retrying snmpget for %s oid %s with SNMP v1", ip, oid)
        value = run_snmp_command(ip, community, oid, "1", "snmpget")
    return value

def run_snmp_command(ip, community, oid, version, command_type):
    cmd = [f"/usr/bin/{command_type}", f"-v{version}", "-c", community, ip, oid]
    try:
        logger.debug("Running %s against %s with SNMP v%s for oid %s", command_type, ip, version, oid)
        result = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode(errors='ignore')
        
        if command_type == "snmpget":
            value = result.split(":")[-1].strip()
            if "(" in value and ")" in value:
                value = value.split("(")[-1].split(")")[0]
            return value
        return result
    except subprocess.TimeoutExpired:
        logger.debug("%s timed out for %s with SNMP v%s and oid %s", command_type, ip, version, oid)
    except subprocess.CalledProcessError:
        logger.debug("%s returned non-zero exit status for %s with SNMP v%s and oid %s", command_type, ip, version, oid)
    except FileNotFoundError:
        logger.exception("SNMP command %s is not available at expected path", command_type)
    except Exception:
        logger.exception("Unexpected error running %s for %s with SNMP v%s and oid %s", command_type, ip, version, oid)
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
        logger.info("Email alerts disabled; skipping alert email for %s", printer_name)
        return

    msg = EmailMessage()
    msg.set_content(f"CRITICAL PRINTER ALERT\n\nPrinter: {printer_name}\nIP Address: {ip_address}\nError: {error_text}\n\nPlease check the dashboard for more details or click here to access the printer UI: http://{ip_address}")

    msg['Subject'] = f"🚨 Printer Alert: {printer_name} requires attention"
    msg['From'] = SENDER_EMAIL
    msg['To'] = ", ".join(RECIPIENT_EMAILS)
    smtp_local_hostname = get_smtp_local_hostname(SENDER_EMAIL)

    logger.info(
        "Sending alert email for %s via %s:%s from %s using EHLO %s",
        printer_name,
        SMTP_SERVER,
        SMTP_PORT,
        SENDER_EMAIL,
        smtp_local_hostname,
    )
    logger.debug(
        "Alert email recipients for %s: %s",
        printer_name,
        ", ".join(RECIPIENT_EMAILS),
    )

    try:
        with InstrumentedSMTP(local_hostname=smtp_local_hostname) as server:
            server.configure_protocol_logging(SMTP_LOG_PROTOCOL, logger, printer_name)
            connect_code, connect_response = server.connect(SMTP_SERVER, SMTP_PORT)
            logger.debug(
                "SMTP connect completed for %s with code=%s response=%s local_hostname=%s peer=%s sockname=%s",
                printer_name,
                connect_code,
                format_smtp_response(connect_response),
                server.local_hostname,
                server.sock.getpeername() if server.sock else None,
                server.sock.getsockname() if server.sock else None,
            )
            ehlo_code, ehlo_response = server.ehlo()
            logger.debug(
                "SMTP EHLO completed for %s with code=%s response=%s does_esmtp=%s features=%s",
                printer_name,
                ehlo_code,
                format_smtp_response(ehlo_response),
                server.does_esmtp,
                server.esmtp_features,
            )
            if SMTP_STARTTLS:
                starttls_supported = server.has_extn("starttls")
                logger.debug(
                    "SMTP STARTTLS availability for %s: requested=%s required=%s supported=%s",
                    printer_name,
                    SMTP_STARTTLS,
                    SMTP_REQUIRE_STARTTLS,
                    starttls_supported,
                )
                if not starttls_supported:
                    if SMTP_REQUIRE_STARTTLS:
                        raise smtplib.SMTPNotSupportedError("SMTP server does not advertise STARTTLS")
                else:
                    starttls_code, starttls_response = server.starttls(context=ssl.create_default_context())
                    logger.debug(
                        "SMTP STARTTLS completed for %s with code=%s response=%s",
                        printer_name,
                        starttls_code,
                        format_smtp_response(starttls_response),
                    )
                    ehlo_code, ehlo_response = server.ehlo()
                    logger.debug(
                        "SMTP post-STARTTLS EHLO completed for %s with code=%s response=%s does_esmtp=%s features=%s",
                        printer_name,
                        ehlo_code,
                        format_smtp_response(ehlo_response),
                        server.does_esmtp,
                        server.esmtp_features,
                    )
            server.send_message(msg, from_addr=SENDER_EMAIL, to_addrs=RECIPIENT_EMAILS)
        logger.info("Alert email sent for %s", printer_name)
    except smtplib.SMTPSenderRefused as exc:
        logger.error(
            "SMTP sender refused for %s with code=%s sender=%s ehlo=%s response=%s",
            printer_name,
            exc.smtp_code,
            exc.sender,
            smtp_local_hostname,
            format_smtp_response(exc.smtp_error),
        )
        logger.exception("Failed to send alert email for %s", printer_name)
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
        <title>Office Printer Dashboard</title>
        <style>
            /* CSS Variables for Dark/Light Mode */
            :root {
                --bg-color: #f4f7f6;
                --text-color: #333;
                --table-bg: #fff;
                --table-border: #eee;
                --th-bg: #34495e;
                --th-text: #fff;
                --row-even: #f9fbfc;
                --row-hover: #f1f1f1;
                --link-color: #2980b9;
                --link-hover: #1abc9c;
                --shop-th: #2980b9;
                --bar-bg: #e0e0e0;
                --header-border: #3498db;
                
                --toner-black: #34495e;
                --toner-cyan: #00bcd4;
                --toner-magenta: #e91e63;
                --toner-yellow: #f1c40f;
                --toner-fuser: #95a5a6;
            }
            [data-theme="dark"] {
                --bg-color: #121212;
                --text-color: #e0e0e0;
                --table-bg: #1e1e1e;
                --table-border: #333;
                --th-bg: #1f2b38;
                --th-text: #e0e0e0;
                --row-even: #252525;
                --row-hover: #2c2c2c;
                --link-color: #64b5f6;
                --link-hover: #81c784;
                --shop-th: #154360;
                --bar-bg: #444;
                --header-border: #64b5f6;
                
                --toner-black: #9aa7b1; 
                --toner-cyan: #26c6da;
                --toner-magenta: #ec407a;
                --toner-yellow: #ffee58;
                --toner-fuser: #7f8c8d;
            }
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 20px; background-color: var(--bg-color); color: var(--text-color); transition: background 0.3s, color 0.3s; }
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
            .online { color: #2ecc71; font-weight: bold; }
            .shopping-table th { background-color: var(--shop-th); }
            
            .alert-critical { background-color: #ffcccc; color: black; font-weight: 500; padding: 4px 8px; border-radius: 4px; display: inline-block;}
            .alert-warning { background-color: #ffe0b2; color: black; font-weight: 500; padding: 4px 8px; border-radius: 4px; display: inline-block;}
            
            /* Theme Toggle Button */
            .theme-toggle { background: var(--th-bg); color: var(--th-text); border: none; padding: 10px 15px; border-radius: 20px; cursor: pointer; font-weight: bold; transition: 0.3s; }
            .theme-toggle:hover { background: var(--link-hover); color: #fff;}
            
            /* Progress Bars */
            .progress-container { width: 100%; min-width: 60px; background-color: var(--bar-bg); border-radius: 4px; position: relative; height: 22px; display: flex; align-items: center; overflow: hidden; border: 1px solid rgba(0,0,0,0.1);}
            .progress-bar { height: 100%; transition: width 0.5s ease; }
            .progress-text { position: absolute; width: 100%; text-align: center; font-size: 0.85em; font-weight: 700; color: #fff; text-shadow: 1px 1px 3px rgba(0,0,0,0.9), -1px -1px 3px rgba(0,0,0,0.9); pointer-events: none;}
        </style>
        
        <script>
            // --- SMART EMAIL SIGNATURE LOGIC ---
            function resetName() {
                localStorage.removeItem('orderName');
                alert("Signature name has been reset! You will be prompted for your name the next time you click to order.");
            }

            function sendOrderEmail(vendor, printerName, serial) {
                let userName = localStorage.getItem('orderName');
                if (!userName) {
                    userName = prompt("First time setup: What is your name? (This will be used for the email signature)");
                    if (userName && userName.trim() !== "") {
                        localStorage.setItem('orderName', userName.trim());
                    } else {
                        userName = "IT Support"; 
                    }
                }

                let subject = "Toner Order - " + serial;
                let printerDesc = printerName === 'Mercury' ? serial : "serial number " + serial;
                
                let fullMessage = `Dear sir/madam,

Our printer (${printerDesc}) has run out of toner. We need a full set of colors and two blacks.

--
${userName}
IT Support Team
T: (514) 861-4367

GOLDWATER DROIT
3500 ouest boul. de Maisonneuve Blvd. West
Suite 2310, Office Tower 2
Westmount, QC H3Z 3C1`;

                let fullBody = encodeURIComponent(fullMessage);
                let encodedSubject = encodeURIComponent(subject);
                let emailAddress = vendor === 'canon' ? 'canonsupplies@canada.canon.com' : 'cmorrison@printersplus.net';

                window.location.href = "mailto:" + emailAddress + "?subject=" + encodedSubject + "&body=" + fullBody;
            }

            // Dark Mode Logic
            if (localStorage.getItem('theme') === 'dark') {
                document.documentElement.setAttribute('data-theme', 'dark');
            }
            function toggleDark() {
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
                localStorage.setItem('theme', isDark ? 'light' : 'dark');
            }

            // Smooth Background Refresh
            function startAutoRefresh() {
                setInterval(async () => {
                    try {
                        const response = await fetch(window.location.href);
                        const html = await response.text();
                        const parser = new DOMParser();
                        const doc = parser.parseFromString(html, 'text/html');
                        
                        document.getElementById('live-content').innerHTML = doc.getElementById('live-content').innerHTML;
                    } catch (e) { console.error("Auto-refresh failed", e); }
                }, 300000); 
            }

            // Table Sorting Logic
            function sortTable(tableId, colIndex) {
                var table, rows, switching, i, x, y, shouldSwitch, dir, switchcount = 0;
                table = document.getElementById(tableId);
                switching = true;
                dir = "asc"; 
                while (switching) {
                    switching = false;
                    rows = table.rows;
                    for (i = 1; i < (rows.length - 1); i++) {
                        shouldSwitch = false;
                        x = rows[i].getElementsByTagName("TD")[colIndex];
                        y = rows[i + 1].getElementsByTagName("TD")[colIndex];
                        
                        let valX = x.innerText || x.textContent;
                        let valY = y.innerText || y.textContent;
                        
                        let numX = parseFloat(valX.replace(/[^0-9.-]/g, ''));
                        let numY = parseFloat(valY.replace(/[^0-9.-]/g, ''));
                        
                        if (!isNaN(numX) && !isNaN(numY) && valX.includes("%")) {
                            valX = numX; valY = numY;
                        } else {
                            valX = valX.toLowerCase(); valY = valY.toLowerCase();
                        }

                        if (dir == "asc") {
                            if (valX > valY) { shouldSwitch = true; break; }
                        } else if (dir == "desc") {
                            if (valX < valY) { shouldSwitch = true; break; }
                        }
                    }
                    if (shouldSwitch) {
                        rows[i].parentNode.insertBefore(rows[i + 1], rows[i]);
                        switching = true;
                        switchcount ++;      
                    } else {
                        if (switchcount == 0 && dir == "asc") {
                            dir = "desc";
                            switching = true;
                        }
                    }
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
                    <button class="theme-toggle" onclick="resetName()" style="margin-right: 10px;">👤 Reset Signature Name</button>
                    <button class="theme-toggle" onclick="toggleDark()">🌙 / ☀️ Toggle Theme</button>
                </div>
            </div>
    """
    
    html += f"""
            <div id="live-content">
                <div class="timestamp">Last Updated: {timestamp}</div>
                
                <h2>🛒 Shopping List</h2>
                <table class="shopping-table" id="shopTable">
                    <tr>
                        <th onclick="sortTable('shopTable', 0)">Model / Group ↕</th>
                        <th onclick="sortTable('shopTable', 1)">Black Qty ↕</th>
                        <th onclick="sortTable('shopTable', 2)">Cyan Qty ↕</th>
                        <th onclick="sortTable('shopTable', 3)">Magenta Qty ↕</th>
                        <th onclick="sortTable('shopTable', 4)">Yellow Qty ↕</th>
                        <th onclick="sortTable('shopTable', 5)">Fuser Qty ↕</th>
                        <th onclick="sortTable('shopTable', 6)">Waste Qty ↕</th>
                        <th>Parts to Order</th>
                        <th>Order Here</th>
                    </tr>
    """

    sorted_models = sorted(shopping_counts.keys(), key=lambda x: x.lower())
    for model in sorted_models:
        counts = shopping_counts[model]
        if sum(counts.values()) > 0:
            
            parts_to_order = []
            model_parts = PART_NUMBERS.get(model, {})
            
            for item in ["Black", "Cyan", "Magenta", "Yellow", "Fuser", "Waste"]:
                if counts[item] > 0:
                    part_no = model_parts.get(item, "?")
                    parts_to_order.append(f"{item}: {part_no}")
            
            parts_str = "<br>".join(parts_to_order) if parts_to_order else "None needed yet"
            
            # Smart Order Links
            order_link = ""
            if "Galactica" in model:
                order_link = f"<a href='#' onclick=\"sendOrderEmail('canon', 'Galactica', '2XK0 2732'); return false;\">📧 Email Canon</a>"
            elif "Orion" in model:
                order_link = f"<a href='#' onclick=\"sendOrderEmail('canon', 'Orion', '3TQ0 1854'); return false;\">📧 Email Canon</a>"
            elif "Mercury" in model:
                order_link = f"<a href='#' onclick=\"sendOrderEmail('printersplus', 'Mercury', 'Xerox AltaLink C8145'); return false;\">📧 Email PrintersPlus</a>"
            else:
                order_link = "<a href='https://www.printersplus.net' target='_blank'>🌐 printersplus.net</a>"
            
            html += f"""
                    <tr>
                        <td><strong>{model}</strong></td>
                        <td>{counts['Black']}</td>
                        <td>{counts['Cyan']}</td>
                        <td>{counts['Magenta']}</td>
                        <td>{counts['Yellow']}</td>
                        <td>{counts['Fuser']}</td>
                        <td>{counts['Waste']}</td>
                        <td>{parts_str}</td>
                        <td>{order_link}</td>
                    </tr>
            """

    html += """
                </table>
                
                <h2>📊 Live Printer Status</h2>
                <table id="statusTable">
                    <tr>
                        <th onclick="sortTable('statusTable', 0)">Name ↕</th>
                        <th onclick="sortTable('statusTable', 1)">IP Address ↕</th>
                        <th onclick="sortTable('statusTable', 2)">Model ↕</th>
                        <th onclick="sortTable('statusTable', 3)">Status ↕</th>
                        <th onclick="sortTable('statusTable', 4)">Current Alerts ↕</th>
                        <th onclick="sortTable('statusTable', 5)">Black ↕</th>
                        <th onclick="sortTable('statusTable', 6)">Cyan ↕</th>
                        <th onclick="sortTable('statusTable', 7)">Magenta ↕</th>
                        <th onclick="sortTable('statusTable', 8)">Yellow ↕</th>
                        <th onclick="sortTable('statusTable', 9)">Fuser ↕</th>
                        <th onclick="sortTable('statusTable', 10)">Waste ↕</th>
                    </tr>
    """

    def format_supply(val, is_waste=False, color_type="black"):
        if val == "" or val is None: return "-"
        if val in ["OK", "?"]: return val
        if val == "REPLACE": return "<span class='offline'>REPLACE</span>"
        if isinstance(val, int):
            
            bar_color = "var(--toner-fuser)"
            if color_type == "black": bar_color = "var(--toner-black)"
            elif color_type == "cyan": bar_color = "var(--toner-cyan)"
            elif color_type == "magenta": bar_color = "var(--toner-magenta)"
            elif color_type == "yellow": bar_color = "var(--toner-yellow)"
            
            actual_val = 0 if val == -2 else val
            
            if is_waste:
                if actual_val >= WASTE_FULL_THRESHOLD:
                    bar_color = "#e74c3c" 
                else:
                    return "OK"
            
            display = f"{actual_val}%"
            width_percent = actual_val if actual_val >= 0 else 0
                
            return f"<div class='progress-container'><div class='progress-bar' style='width: {width_percent}%; background-color: {bar_color};'></div><span class='progress-text'>{display}</span></div>"
            
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
                        <td><span {alerts_td_class_attr}>{alerts_td_content}</span></td>
                        <td>{format_supply(p['b'], color_type='black')}</td>
                        <td>{format_supply(p['c'], color_type='cyan')}</td>
                        <td>{format_supply(p['m'], color_type='magenta')}</td>
                        <td>{format_supply(p['y'], color_type='yellow')}</td>
                        <td>{format_supply(p['fuser'], color_type='fuser')}</td>
                        <td>{format_supply(p['waste'], is_waste=True, color_type='waste')}</td>
                    </tr>
        """

    html += """
                </table>
            </div> </div>
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
    logger.info("Starting dashboard generation using CSV=%s STATE=%s HTML=%s", CSV_FILE, STATE_FILE, HTML_FILE)
    
    shopping_counts = {}
    
    alert_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                alert_state = json.load(f)
            logger.debug("Loaded %s existing alert-state entries from %s", len(alert_state), STATE_FILE)
        except Exception:
            logger.exception("Failed to load alert state from %s; continuing with empty state", STATE_FILE)
    else:
        logger.debug("No existing alert state file found at %s", STATE_FILE)

    current_broken_printers = []

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        logger.info("Loaded %s printer definitions from %s", len(rows), CSV_FILE)
        logger.info("Starting printer polling loop for %s printers", len(rows))
        for row in rows:
            ip, name, community = row["IP"], row["name"], row["community"]
            original_model = row.get("model", "Unknown Model")
            logger.debug("Polling printer %s at %s (model=%s)", name, ip, original_model)
            
            shopping_model_name = SUPPLY_MAPPING.get(original_model, original_model)

            if shopping_model_name not in shopping_counts:
                shopping_counts[shopping_model_name] = {'Black': 0, 'Cyan': 0, 'Magenta': 0, 'Yellow': 0, 'Fuser': 0, 'Waste': 0}

            dev_stat = snmp_get(ip, community, get_oid(original_model, "device_status"))
            
            online = "YES" if dev_stat else "NO"
            state = map_status(dev_stat)
            
            alerts_data = get_printer_alerts(ip, community)

            # --- FETCH ALL SUPPLIES BEFORE CHECKING FOR CRITICAL ALERTS ---
            b = get_supply_percent(ip, community, original_model, "black")
            c = get_supply_percent(ip, community, original_model, "cyan")
            m = get_supply_percent(ip, community, original_model, "magenta")
            y = get_supply_percent(ip, community, original_model, "yellow")
            fuser = get_supply_percent(ip, community, original_model, "fuser")
            waste_raw = get_supply_percent(ip, community, original_model, "waste")

            # --- THE FIX: Failsafe now flags both 0 AND -2 (Error) ---
            failsafe_errors = []
            for color_name, val in [("Black Toner", b), ("Cyan Toner", c), ("Magenta Toner", m), ("Yellow Toner", y), ("Fuser", fuser)]:
                if isinstance(val, int) and (val == 0 or val == -2):
                    failsafe_errors.append(f"{color_name} is empty (0%)")
            
            # --- OVERRIDE DASHBOARD ALERTS WITH FAILSAFE ---
            if failsafe_errors:
                alerts_data['is_critical'] = True
                if alerts_data['text']:
                    alerts_data['text'] = alerts_data['text'] + " | " + " | ".join(failsafe_errors)
                else:
                    alerts_data['text'] = " | ".join(failsafe_errors)

            if alerts_data['is_critical']:
                current_broken_printers.append(name) 
                
                # Check if it's a new broken printer, OR if the error message has changed!
                if name not in alert_state or alert_state[name] != alerts_data['text']:
                    logger.info("New or updated critical alert detected for %s; sending notification", name)
                    send_critical_email(name, ip, alerts_data['text'])
                    alert_state[name] = alerts_data['text'] # Save the EXACT error text
                else:
                    logger.debug("Critical alert for %s already recorded in alert state", name)
                
                    

            waste_alert_keywords = ["replace toner collection unit", "waste toner", "collection bin", "collection unit"]
            if any(kw in alerts_data['text'].lower() for kw in waste_alert_keywords):
                waste_raw = "REPLACE"
                waste_display = "REPLACE"
            else:
                waste_display = format_waste(waste_raw)

            for color, val in [("Black", b), ("Cyan", c), ("Magenta", m), ("Yellow", y)]:
                if isinstance(val, int) and (val <= TONER_LOW_THRESHOLD or val == -2):
                    shopping_counts[shopping_model_name][color] += 1
            
            if isinstance(fuser, int) and (fuser <= TONER_LOW_THRESHOLD or fuser == 0 or fuser == -2):
                shopping_counts[shopping_model_name]['Fuser'] += 1

            if (isinstance(waste_raw, int) and waste_raw >= WASTE_FULL_THRESHOLD) or waste_raw == "REPLACE":
                shopping_counts[shopping_model_name]['Waste'] += 1

            printer_data.append({
                "name": name, "ip": ip, "model": original_model,
                "online": online, "state": state,
                "alerts": alerts_data, 
                "b": b, "c": c, "m": m, "y": y, "fuser": fuser, "waste": waste_display
            })
            logger.debug(
                "Completed polling for %s: online=%s state=%s critical=%s warning=%s",
                name,
                online,
                state,
                alerts_data['is_critical'],
                alerts_data['is_warning'],
            )
        logger.info("Completed printer polling loop for %s printers", len(printer_data))

    printers_to_remove = [p for p in alert_state if p not in current_broken_printers]
    for p in printers_to_remove:
        del alert_state[p]
    if printers_to_remove:
        logger.info("Cleared %s resolved printer alerts from state", len(printers_to_remove))
        
    with open(STATE_FILE, "w") as f:
        json.dump(alert_state, f)
    logger.debug("Wrote %s alert-state entries to %s", len(alert_state), STATE_FILE)

    printer_data.sort(key=lambda x: x['name'].lower())

    timestamp = format_display_timestamp(get_local_now())
    
    generate_html(printer_data, shopping_counts, timestamp)
    logger.info(
        "Dashboard generated successfully at %s with %s printers and %s active critical alerts",
        HTML_FILE,
        len(printer_data),
        len(current_broken_printers),
    )

if __name__ == "__main__":
    main()