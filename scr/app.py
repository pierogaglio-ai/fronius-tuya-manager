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
            last_status_refresh = time.time()

        with runtime_lock:
            is_auto_mode = auto_mode
            cfg = thresholds.copy()

        if start_time <= now <= end_time and is_auto_mode:
            try:
                power = fetch_inverter_data(timeout=4)
                with runtime_lock:
                    latest_power_data.update(power)

                with runtime_lock:
                    current_states = {
                        "StufaP": device_states["StufaP"]["is_on"],
                        "StufaG": device_states["StufaG"]["is_on"],
                    }

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
    device = data.get("device")
    command = data.get("command")

    if device == "auto":
        with runtime_lock:
            auto_mode = command == "on"
            value = auto_mode
        logging.info("Modalità automatica impostata su: %s", value)
        return jsonify({"auto_mode": value})

    if device in DEVICES and command in {"on", "off"}:
        set_device_state(device, command == "on")
        return jsonify({"ok": True})

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
    <html>
    <head>
        <title>Fronius Monitor</title>
        <meta charset="utf-8" />
        <style>
            body { font-family: Arial; background: #f4f4f4; padding: 20px; }
            h1 { color: #333; }
            #data { background: #fff; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom:20px; }
            .item { margin: 8px 0; }
            .status { padding: 5px 10px; border-radius: 6px; display:inline-block; background:#999; color:white; }
            .controls button { margin: 5px; padding: 10px 15px; border: none; border-radius: 6px; cursor: pointer; }
            .controls .on { background:#4caf50; color:white; }
            .controls .off { background:#f44336; color:white; }
            .controls .auto { background:#2196f3; color:white; }
            .thresholds input { width: 80px; margin: 5px; }
        </style>
    </head>
    <body>
        <h1>Fronius Monitor</h1>
        <div id="data">Caricamento...</div>
        <h2>Controllo manuale</h2>
        <div class="controls">
            <div><b>StufaP:</b>
                <button class="on" onclick="sendCmd('StufaP','on')">ON</button>
                <button class="off" onclick="sendCmd('StufaP','off')">OFF</button>
                <span id="stato-StufaP" class="status">?</span>
            </div>
            <div><b>StufaG:</b>
                <button class="on" onclick="sendCmd('StufaG','on')">ON</button>
                <button class="off" onclick="sendCmd('StufaG','off')">OFF</button>
                <span id="stato-StufaG" class="status">?</span>
            </div>
        </div>
        <h2>Automatismo</h2>
        <button class="auto" onclick="toggleAuto()">Attiva/Disattiva Automatico</button>
        <div>Stato automatico: <span id="auto_mode">?</span></div>
        <h3>Soglie</h3>
        <div class="thresholds">
            X: <input id="X" type="number"> W <br>
            Y: <input id="Y" type="number"> W <br>
            Z: <input id="Z" type="number"> W <br>
            D: <input id="D" type="number"> W <br>
            <button onclick="saveThresholds()">Salva</button>
        </div>
        <script>
            async function fetchData() {
                try {
                    const res = await fetch('/data');
                    const json = await res.json();
                    document.getElementById('data').innerHTML = `
                        <div class="item"><b>Produzione FV:</b> ${json.produzione} W</div>
                        <div class="item"><b>Consumo Casa:</b> ${json.consumo} W</div>
                        <div class="item"><b>Immissione in Rete:</b> ${json.rete} W</div>
                        <div class="item"><b>SOC Batteria:</b> ${json.soc} %</div>
                    `;
                    for (let dev in json.stati) {
                        let el = document.getElementById("stato-" + dev);
                        el.innerText = json.stati[dev];
                    }
                    document.getElementById("auto_mode").innerText = json.auto_mode ? "ON" : "OFF";
                    for (let k in json.thresholds) {
                        document.getElementById(k).value = json.thresholds[k];
                    }
                } catch (e) {
                    document.getElementById('data').innerText = 'Errore di connessione';
                }
            }
            function sendCmd(device, cmd) {
                fetch('/control', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({device:device, command:cmd})
                }).then(fetchData);
            }
            function toggleAuto() {
                const cmd = document.getElementById("auto_mode").innerText === "ON" ? "off" : "on";
                fetch('/control', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({device:"auto", command:cmd})
                }).then(fetchData);
            }
            function saveThresholds() {
                let payload = {
                    X: parseInt(document.getElementById("X").value),
                    Y: parseInt(document.getElementById("Y").value),
                    Z: parseInt(document.getElementById("Z").value),
                    D: parseInt(document.getElementById("D").value)
                };
                fetch('/set_thresholds', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
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
