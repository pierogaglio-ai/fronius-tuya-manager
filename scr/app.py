###### versione 2.1 - ottimizzata per esecuzione continua su Termux in LAN locale
from flask import Flask, jsonify, render_template_string, request
from tuya_connector import TuyaOpenAPI

import datetime
import logging
import os
import threading
import time

import requests

# Configurazione x Inverter Fronius Gen24Plus
INVERTER_IP = os.getenv("INVERTER_IP", "192.168.1.100")

# Credenziali Tuya e dati dispositivi
API_ENDPOINT = os.getenv("TUYA_API_ENDPOINT", "https://openapi.tuyaeu.com")
ACCESS_ID = os.getenv("TUYA_ACCESS_ID", "tuo access id tuya")
ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET", "tuo access secret tuya")

DEVICE_ID = os.getenv("TUYA_DEVICE_ID_STUFAG", "tuo devicess id 1 tuya")
DEVICE_ID2 = os.getenv("TUYA_DEVICE_ID_STUFAP", "tuo devicess id 2 tuya")

DEVICES = {
    "StufaP": DEVICE_ID2,
    "StufaG": DEVICE_ID,
}

THERMOMETER_DEVICE_ID = os.getenv("TUYA_DEVICE_ID_TERMOMETRO", "")

# Polling e finestra automazione (ottimizzati per stabilità su Termux)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
STATUS_REFRESH_SECONDS = int(os.getenv("STATUS_REFRESH_SECONDS", "60"))
START_HOUR = int(os.getenv("AUTO_START_HOUR", "11"))
START_MINUTE = int(os.getenv("AUTO_START_MINUTE", "0"))
END_HOUR = int(os.getenv("AUTO_END_HOUR", "17"))
END_MINUTE = int(os.getenv("AUTO_END_MINUTE", "30"))

# Connessione all'API Tuya
openapi = TuyaOpenAPI(API_ENDPOINT, ACCESS_ID, ACCESS_SECRET)
openapi.connect()

# Sessione HTTP persistente verso inverter per ridurre overhead rete/CPU
inverter_session = requests.Session()

app = Flask(__name__)

# Stato runtime condiviso
runtime_lock = threading.Lock()
auto_mode = True
thresholds = {"X": 500, "Y": 1000, "Z": 800, "D": 200}

latest_power_data = {
    "produzione": 0,
    "consumo": 0,
    "rete": 0,
    "soc": 0,
    "timestamp": 0,
}

device_states = {
    "StufaP": {"is_on": None, "text": "⚠️ Stato StufaP non disponibile"},
    "StufaG": {"is_on": None, "text": "⚠️ Stato StufaG non disponibile"},
}

thermometer_data = {
    "available": bool(THERMOMETER_DEVICE_ID),
    "name": "Termometro",
    "temperature_c": None,
    "humidity": None,
    "updated_at": 0,
    "warning": "Termometro non configurato" if not THERMOMETER_DEVICE_ID else None,
}

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def tuya_request_with_reconnect(request_func, *args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            return request_func(*args, **kwargs)
        except Exception as e:
            logging.warning("[Tuya] Errore: %s (tentativo %s/%s)", e, attempt + 1, max_retries)
            if "token" in str(e).lower() or "auth" in str(e).lower() or attempt == max_retries - 1:
                try:
                    logging.info("[Tuya] Riconnessione...")
                    openapi.connect()
                    time.sleep(1)
                except Exception as reconn_error:
                    logging.error("[Tuya] Errore nella riconnessione: %s", reconn_error)
                    if attempt == max_retries - 1:
                        raise
            elif attempt == max_retries - 1:
                raise
            else:
                time.sleep(1)


def inverter_url():
    return f"http://{INVERTER_IP}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"


def fetch_inverter_data(timeout=4):
    r = inverter_session.get(inverter_url(), timeout=timeout)
    data = r.json()
    site = data["Body"]["Data"]["Site"]
    inverter = data["Body"]["Data"]["Inverters"].get("1", {})
    return {
        "produzione": int(site.get("P_PV", 0)),
        "consumo": int(site.get("P_Load", 0)),
        "rete": int(site.get("P_Grid", 0) * -1),
        "soc": int(inverter.get("SOC", 0)),
        "timestamp": int(time.time()),
    }


def parse_device_status(device_name, status_response):
    for item in status_response.get("result", []):
        if item.get("code") == "switch_1":
            is_on = bool(item.get("value"))
            return {
                "is_on": is_on,
                "text": f"🔥 {device_name} è ACCESA" if is_on else f"❄️ {device_name} è SPENTA",
            }
    return {"is_on": None, "text": f"⚠️ Stato {device_name} non riconosciuto"}


def refresh_all_device_states():
    for device_name, device_id in DEVICES.items():
        try:
            status = tuya_request_with_reconnect(openapi.get, f"/v1.0/iot-03/devices/{device_id}/status")
            parsed = parse_device_status(device_name, status)
            with runtime_lock:
                device_states[device_name] = parsed
        except Exception as e:
            logging.error("Errore lettura stato %s: %s", device_name, e)


def _scaled_number(raw_value):
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except Exception:
        return None
    if value > 1000:
        return value / 100
    if value > 100:
        return value / 10
    return value


def parse_thermometer_status(status_response):
    result = {"temperature_c": None, "humidity": None}
    for item in status_response.get("result", []):
        code = item.get("code")
        value = item.get("value")
        if code in {"temp_current", "va_temperature", "cur_temperature", "temperature"}:
            result["temperature_c"] = _scaled_number(value)
        elif code in {"humidity_value", "va_humidity", "cur_humidity", "humidity"}:
            result["humidity"] = _scaled_number(value)
    return result


def refresh_thermometer_state():
    if not THERMOMETER_DEVICE_ID:
        return

    try:
        status = tuya_request_with_reconnect(openapi.get, f"/v1.0/iot-03/devices/{THERMOMETER_DEVICE_ID}/status")
        parsed = parse_thermometer_status(status)
        with runtime_lock:
            thermometer_data.update(
                {
                    "available": True,
                    "temperature_c": parsed["temperature_c"],
                    "humidity": parsed["humidity"],
                    "updated_at": int(time.time()),
                    "warning": None if parsed["temperature_c"] is not None or parsed["humidity"] is not None else "Nessun dato termometro trovato nello status Tuya",
                }
            )
    except Exception as e:
        logging.error("Errore lettura termometro: %s", e)
        with runtime_lock:
            thermometer_data.update({"available": True, "warning": str(e)})


def set_device_state(device_name, desired_on):
    device_id = DEVICES[device_name]

    with runtime_lock:
        known_state = device_states[device_name]["is_on"]

    if known_state is desired_on:
        return

    try:
        tuya_request_with_reconnect(
            openapi.post,
            f"/v1.0/iot-03/devices/{device_id}/commands",
            {"commands": [{"code": "switch_1", "value": desired_on}]},
        )
        with runtime_lock:
            device_states[device_name] = {
                "is_on": desired_on,
                "text": f"🔥 {device_name} è ACCESA" if desired_on else f"❄️ {device_name} è SPENTA",
            }
    except Exception:
        logging.warning("⚠️ Codice 'switch_1' non trovato o dispositivo non rintracciabile")


def compute_targets(p_grid, cfg, current_states):
    # Isteresi desiderata:
    # - Accensione con soglie alte (X/Y)
    # - Spegnimento con soglie basse (D/Z)
    # In mezzo alle soglie mantiene lo stato precedente.
    target = {
        "StufaP": bool(current_states.get("StufaP")),
        "StufaG": bool(current_states.get("StufaG")),
    }

    # Accensione
    if p_grid > cfg["X"]:
        target["StufaP"] = True
    if p_grid > cfg["Y"]:
        target["StufaG"] = True

    # Spegnimento
    if p_grid < cfg["Z"]:
        target["StufaG"] = False
    if p_grid < cfg["D"]:
        target["StufaP"] = False

    return target


def automazione_loop():
    start_time = datetime.time(START_HOUR, START_MINUTE)
    end_time = datetime.time(END_HOUR, END_MINUTE)
    last_status_refresh = 0

    while True:
        now = datetime.datetime.now().time()

        if time.time() - last_status_refresh >= STATUS_REFRESH_SECONDS:
            refresh_all_device_states()
            refresh_thermometer_state()
            last_status_refresh = time.time()

        with runtime_lock:
            is_auto_mode = auto_mode
            cfg = thresholds.copy()
            current_states = {name: state["is_on"] for name, state in device_states.items()}

        if start_time <= now <= end_time and is_auto_mode:
            try:
                power = fetch_inverter_data(timeout=4)
                with runtime_lock:
                    latest_power_data.update(power)

                targets = compute_targets(power["rete"], cfg, current_states)
                set_device_state("StufaP", targets["StufaP"])
                set_device_state("StufaG", targets["StufaG"])

                logging.info("Automazione: rete=%sW target=%s", power["rete"], targets)
            except Exception as e:
                logging.error("Errore automazione: %s", e)

        time.sleep(POLL_SECONDS)


threading.Thread(target=automazione_loop, daemon=True).start()


@app.route("/data")
def get_data():
    with runtime_lock:
        snapshot = {
            "produzione": latest_power_data["produzione"],
            "consumo": latest_power_data["consumo"],
            "rete": latest_power_data["rete"],
            "soc": latest_power_data["soc"],
            "stati": {name: state["text"] for name, state in device_states.items()},
            "auto_mode": auto_mode,
            "thresholds": thresholds.copy(),
            "thermometer": thermometer_data.copy(),
            "updated_at": latest_power_data["timestamp"],
        }

    # Aggiornamento on-demand, ma senza fare polling Tuya ad ogni richiesta
    try:
        power = fetch_inverter_data(timeout=3)
        with runtime_lock:
            latest_power_data.update(power)
            snapshot.update(
                {
                    "produzione": power["produzione"],
                    "consumo": power["consumo"],
                    "rete": power["rete"],
                    "soc": power["soc"],
                    "updated_at": power["timestamp"],
                }
            )
    except Exception as e:
        snapshot["warning"] = f"Dato inverter non aggiornato: {e}"

    return jsonify(snapshot)


@app.route("/control", methods=["POST"])
def control():
    global auto_mode
    data = request.json or {}
    device = str(data.get("device", "")).strip()
    command = str(data.get("command", "")).strip().lower()

    if device == "auto":
        if command not in {"on", "off"}:
            return jsonify({"error": "comando auto non valido"}), 400
        with runtime_lock:
            auto_mode = command == "on"
            value = auto_mode
        logging.info("Modalità automatica impostata su: %s", value)
        return jsonify({"auto_mode": value})

    if device in DEVICES and command in {"on", "off"}:
        logging.info("Comando manuale dispositivo: %s -> %s", device, command)
        set_device_state(device, command == "on")
        return jsonify({"ok": True, "device": device, "command": command})

    return jsonify({"error": "device o comando sconosciuto"}), 400


@app.route("/set_thresholds", methods=["POST"])
def set_thresholds():
    global thresholds
    payload = request.json or {}
    allowed = {"X", "Y", "Z", "D"}

    for key, value in payload.items():
        if key in allowed:
            try:
                payload[key] = int(value)
            except Exception:
                return jsonify({"error": f"Valore non valido per {key}"}), 400

    with runtime_lock:
        thresholds.update({k: v for k, v in payload.items() if k in allowed})
        result = thresholds.copy()

    logging.info("Soglie aggiornate: %s", result)
    return jsonify(result)


@app.route("/")
def index():
    html = """
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <title>Fronius Dashboard</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            :root {
                --bg: #0f172a;
                --card: #111827;
                --line: #1f2937;
                --muted: #94a3b8;
                --text: #e2e8f0;
                --ok: #22c55e;
                --off: #ef4444;
                --accent: #0ea5e9;
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                padding: 24px;
                font-family: Inter, Arial, sans-serif;
                background: radial-gradient(circle at 20% 0%, #1e293b, var(--bg));
                color: var(--text);
            }
            .header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }
            .mobile-stove-strip { display:none; }
            .subtitle { color: var(--muted); font-size: 14px; }
            .badge { background:#1d4ed8; padding:6px 12px; border-radius:999px; font-size:12px; font-weight:700; }
            .grid { display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:14px; }
            .card {
                background: linear-gradient(180deg, #0b1220, var(--card));
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 14px;
                box-shadow: 0 10px 25px rgba(0,0,0,.25);
            }
            .label { color: var(--muted); font-size: 13px; margin-bottom: 6px; }
            .value { font-size: 30px; font-weight: 700; }
            .controls, .thresholds { grid-column: span 2; }
            .status-wrap { display:flex; gap:10px; flex-wrap:wrap; margin:10px 0; }
            .pill { padding:7px 10px; border-radius:999px; font-size:13px; font-weight:700; }
            .pill-on { background: rgba(34,197,94,.2); border:1px solid rgba(34,197,94,.45); color: #86efac; }
            .pill-off { background: rgba(239,68,68,.15); border:1px solid rgba(239,68,68,.45); color: #fca5a5; }
            button {
                border:none; border-radius:10px; cursor:pointer; padding:9px 12px;
                font-weight:700; margin:4px 4px 0 0; color:#fff; background:var(--accent);
            }
            button.secondary { background:#334155; }
            button.ok { background:#16a34a; }
            .th-grid { display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:8px; }
            .th-item input {
                width: 100%; margin-top: 4px; padding: 8px; border-radius: 8px;
                border: 1px solid #334155; background:#0b1220; color: var(--text);
            }
            .warning { color:#fca5a5; font-size: 13px; }
            @media (max-width: 1080px) {
                .grid { grid-template-columns: 1fr 1fr; }
                .controls, .thresholds { grid-column: span 2; }
            }
            @media (max-width: 720px) {
                body { padding: 14px; }
                .header h1 { font-size: 22px; }
                .grid { grid-template-columns: 1fr; }
                .controls, .thresholds { grid-column: span 1; }
                .mobile-stove-strip {
                    display:flex;
                    gap:8px;
                    position: sticky;
                    top: 8px;
                    z-index: 50;
                    margin-bottom: 10px;
                }
                .mobile-stove-strip .pill {
                    flex:1;
                    text-align:center;
                    background: #111827;
                    border:1px solid #334155;
                }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1 style="margin:0;">Mio HUB</h1>
            </div>
            <div id="autoBadge" class="badge">AUTO: ?</div>
        </div>

        <div class="mobile-stove-strip">
            <div id="stato-StufaP-mobile" class="pill">StufaP: ?</div>
            <div id="stato-StufaG-mobile" class="pill">StufaG: ?</div>
        </div>

        <div class="grid">
            <div class="card"><div class="label">Produzione FV</div><div id="pv" class="value">-- W</div></div>
            <div class="card"><div class="label">Consumo Casa</div><div id="load" class="value">-- W</div></div>
            <div class="card"><div class="label">Immissione in Rete</div><div id="grid" class="value">-- W</div></div>
            <div class="card"><div class="label">Batteria SOC</div><div id="soc" class="value">-- %</div></div>

            <div class="card">
                <div class="label">Termometro ambiente</div>
                <div id="temp" class="value" style="font-size:26px;">-- °C</div>
                <div id="hum" class="subtitle">Umidità: -- %</div>
                <div id="thermoWarn" class="warning"></div>
            </div>

            <div class="card controls">
                <div class="label">Dispositivi</div>
                <div class="status-wrap">
                    <div id="stato-StufaP" class="pill">StufaP: ?</div>
                    <div id="stato-StufaG" class="pill">StufaG: ?</div>
                </div>
                <div>
                    <button type="button" onclick="sendCmd('StufaP','on')">StufaP ON</button>
                    <button type="button" class="secondary" onclick="sendCmd('StufaP','off')">StufaP OFF</button>
                </div>
                <div>
                    <button type="button" onclick="sendCmd('StufaG','on')">StufaG ON</button>
                    <button type="button" class="secondary" onclick="sendCmd('StufaG','off')">StufaG OFF</button>
                </div>
                <div style="margin-top:8px;">
                    <button type="button" id="autoOnBtn" onclick="setAutoMode(true)">Automatico ON</button>
                    <button type="button" id="autoOffBtn" class="secondary" onclick="setAutoMode(false)">Automatico OFF</button>
                </div>
            </div>

            <div class="card thresholds">
                <div class="label">Soglie automazione (W)</div>
                <div class="th-grid">
                    <div class="th-item">X <input id="X" type="number"></div>
                    <div class="th-item">Y <input id="Y" type="number"></div>
                    <div class="th-item">Z <input id="Z" type="number"></div>
                    <div class="th-item">D <input id="D" type="number"></div>
                </div>
                <button type="button" class="ok" style="margin-top:10px;" onclick="saveThresholds()">Salva soglie</button>
            </div>
        </div>

        <script>
            let autoModeState = null;

            function setStatusPill(elementId, text) {
                const el = document.getElementById(elementId);
                el.innerText = text;
                if ((text || "").includes("ACCESA")) {
                    el.className = "pill pill-on";
                } else if ((text || "").includes("SPENTA")) {
                    el.className = "pill pill-off";
                } else {
                    el.className = "pill";
                }
            }

            async function fetchData() {
                try {
                    const res = await fetch('/data');
                    const json = await res.json();

                    document.getElementById('pv').innerText = `${json.produzione} W`;
                    document.getElementById('load').innerText = `${json.consumo} W`;
                    document.getElementById('grid').innerText = `${json.rete} W`;
                    document.getElementById('soc').innerText = `${json.soc} %`;

                    const stufaPText = json.stati?.StufaP || 'StufaP: ?';
                    const stufaGText = json.stati?.StufaG || 'StufaG: ?';
                    setStatusPill('stato-StufaP', stufaPText);
                    setStatusPill('stato-StufaG', stufaGText);
                    setStatusPill('stato-StufaP-mobile', stufaPText);
                    setStatusPill('stato-StufaG-mobile', stufaGText);

                    const t = json.thermometer || {};
                    document.getElementById('temp').innerText = t.temperature_c == null ? '-- °C' : `${t.temperature_c.toFixed(1)} °C`;
                    document.getElementById('hum').innerText = t.humidity == null ? 'Umidità: -- %' : `Umidità: ${t.humidity.toFixed(1)} %`;
                    document.getElementById('thermoWarn').innerText = t.warning || '';

                    autoModeState = Boolean(json.auto_mode);
                    const autoText = autoModeState ? 'AUTO: ON' : 'AUTO: OFF';
                    document.getElementById('autoBadge').innerText = autoText;

                    for (let k in json.thresholds) {
                        const el = document.getElementById(k);
                        if (el) el.value = json.thresholds[k];
                    }
                } catch (e) {
                    document.getElementById('thermoWarn').innerText = 'Errore di connessione al backend';
                }
            }

            function sendCmd(device, cmd) {
                if (!device || device === "auto") return;
                fetch('/control', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({device: device, command: cmd})
                }).then(fetchData);
            }

            function setAutoMode(enabled) {
                const cmd = enabled ? 'on' : 'off';
                fetch('/control', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({device: 'auto', command: cmd})
                }).then(fetchData);
            }

            function saveThresholds() {
                const payload = {
                    X: parseInt(document.getElementById('X').value),
                    Y: parseInt(document.getElementById('Y').value),
                    Z: parseInt(document.getElementById('Z').value),
                    D: parseInt(document.getElementById('D').value)
                };
                fetch('/set_thresholds', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify(payload)
                }).then(fetchData);
            }

            setInterval(fetchData, 30000);
            fetchData();
        </script>
    </body>
    </html>
    """
    return render_template_string(html)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
