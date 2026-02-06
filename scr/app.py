###### versione 2.0 , interroga inverter, usa soglie per attivare stufe, pilota direttamente STUFAG e StufaP collegata a Tuya (smartlife)
from dataclasses import dataclass
import os

from flask import Flask, jsonify, render_template_string, request
from tuya_connector import TuyaOpenAPI

import requests
import threading
import time
import logging
import datetime

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

@dataclass(frozen=True)
class Config:
    inverter_ip: str
    api_endpoint: str
    access_id: str
    access_secret: str
    device_id_stufap: str
    device_id_stufag: str


def _get_config() -> Config:
    return Config(
        inverter_ip=os.getenv("INVERTER_IP", "192.168.1.100"),
        api_endpoint=os.getenv("TUYA_API_ENDPOINT", "https://openapi.tuyaeu.com"),
        access_id=os.getenv("TUYA_ACCESS_ID", ""),
        access_secret=os.getenv("TUYA_ACCESS_SECRET", ""),
        device_id_stufap=os.getenv("TUYA_DEVICE_ID_STUFAP", ""),
        device_id_stufag=os.getenv("TUYA_DEVICE_ID_STUFAG", ""),
    )


CONFIG = _get_config()

DEVICES = {
    "StufaP": CONFIG.device_id_stufap,
    "StufaG": CONFIG.device_id_stufag,
}


def _init_tuya_client() -> TuyaOpenAPI | None:
    if not CONFIG.access_id or not CONFIG.access_secret:
        logging.warning("Credenziali Tuya mancanti: funzionalità Tuya disattivate.")
        return None
    client = TuyaOpenAPI(CONFIG.api_endpoint, CONFIG.access_id, CONFIG.access_secret)
    client.connect()
    return client


openapi = _init_tuya_client()

# Wrapper per richieste Tuya con riconnessione automatica
def tuya_request_with_reconnect(request_func, *args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            return request_func(*args, **kwargs)
        except Exception as e:
            logging.warning(
                "[Tuya] Errore: %s (tentativo %s/%s)",
                e,
                attempt + 1,
                max_retries,
            )
            # Personalizza questa condizione se serve: token, auth, rete, ecc.
            if "token" in str(e).lower() or "auth" in str(e).lower() or attempt == max_retries-1:
                try:
                    if openapi is None:
                        raise RuntimeError("Client Tuya non inizializzato")
                    logging.info("[Tuya] Riconnessione...")
                    openapi.connect()
                    time.sleep(1)
                except Exception as reconn_error:
                    logging.error("[Tuya] Errore nella riconnessione: %s", reconn_error)
                    if attempt == max_retries-1:
                        raise
            else:
                if attempt == max_retries-1:
                    raise
                time.sleep(1)

app = Flask(__name__)
session = requests.Session()
state_lock = threading.Lock()

device_states = {"StufaP": "unknown", "StufaG": "unknown"}
auto_mode = True
thresholds = {"X": 500, "Y": 1000, "Z": 800, "D": 200}  # soglie di accensio spegnimento dispositivi Tuya

# --- Funzioni Gestione prese stufe ---
def ComandoPulsante(device_id, command):
    try:
        if not device_id:
            logging.warning("ID dispositivo Tuya mancante, comando ignorato.")
            return
        if openapi is None:
            logging.warning("Client Tuya non inizializzato, comando ignorato.")
            return
        if command not in {"on", "off"}:
            raise ValueError("Comando non valido")

        value = command == "on"
        tuya_request_with_reconnect(
            openapi.post,
            f"/v1.0/iot-03/devices/{device_id}/commands",
            {"commands": [{"code": "switch_1", "value": value}]},
        )
    except Exception:
        logging.exception(
            "⚠️ Codice 'switch_1' non trovato o dispositivo non rintracciabile"
        )

def StatoDispositivi(device_idx, label):
    if not device_idx or openapi is None:
        return {"state": "unreachable", "label": f"⚠️ {label} non disponibile"}
    try:
        status = tuya_request_with_reconnect(
            openapi.get, f"/v1.0/iot-03/devices/{device_idx}/status"
        )
    except Exception:
        logging.exception("Errore lettura stato Tuya")
        return {"state": "unreachable", "label": f"⚠️ Stato {label} non riconosciuto"}

    for item in status.get("result", []):
        if item.get("code") != "switch_1":
            continue
        if item.get("value") is True:
            return {"state": "on", "label": f"🔥 {label} è ACCESA"}
        if item.get("value") is False:
            return {"state": "off", "label": f"❄️ {label} è SPENTA"}
        return {"state": "unreachable", "label": f"⚠️ Stato {label} non riconosciuto"}
    return {"state": "unreachable", "label": f"⚠️ Stato {label} non riconosciuto"}


def get_powerflow_data():
    url = f"http://{CONFIG.inverter_ip}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    response = session.get(url, timeout=5)
    response.raise_for_status()
    data = response.json()
    site = data["Body"]["Data"]["Site"]
    inverter = data["Body"]["Data"]["Inverters"]["1"]
    p_grid = site["P_Grid"] * -1
    return site, inverter, p_grid

# --- Automazione --- quando va in automatico, ogni 30 secondi dentro la fascia oraria stabilita,
# in base alle soglie di automazione gestisce le due stufe/prese

def automazione_loop():
    start_time = datetime.time(11, 0)
    end_time = datetime.time(17, 30)
    while True:
        now = datetime.datetime.now().time()
        with state_lock:
            current_auto_mode = auto_mode
            current_thresholds = thresholds.copy()
        if start_time <= now <= end_time and current_auto_mode:
            try:
                _, _, p_grid = get_powerflow_data()

                # Accensione
                if p_grid > current_thresholds["Y"]:
                    ComandoPulsante(DEVICES["StufaG"], "on")
                    ComandoPulsante(DEVICES["StufaP"], "on")
                elif p_grid > current_thresholds["X"]:
                    ComandoPulsante(DEVICES["StufaG"], "on")
                    ComandoPulsante(DEVICES["StufaP"], "off")
                else:
                    ComandoPulsante(DEVICES["StufaG"], "off")
                    ComandoPulsante(DEVICES["StufaP"], "off")

                # Spegnimento
                if p_grid < current_thresholds["Z"]:
                    ComandoPulsante(DEVICES["StufaG"], "off")
                    ComandoPulsante(DEVICES["StufaP"], "off")

                if p_grid < current_thresholds["D"]:
                    logging.info("Automazione eseguita con rete: %s W", p_grid)
            except Exception as e:
                logging.error("Errore automazione: %s", e)
        time.sleep(30)

threading.Thread(target=automazione_loop, daemon=True).start()

# --- API ---
@app.route("/data")
def get_data():
    try:
        site, inverter, p_grid = get_powerflow_data()
        with state_lock:
            current_auto_mode = auto_mode
            current_thresholds = thresholds.copy()
        resp = {
            "produzione": site["P_PV"],
            "consumo": site["P_Load"],
            "rete": p_grid,
            "soc": inverter.get("SOC", 0),
            "stati": {
                d: StatoDispositivi(DEVICES[d], d)
                for d in DEVICES
            },
            "auto_mode": current_auto_mode,
            "thresholds": current_thresholds,
        }
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route("/control", methods=["POST"])
def control():
    global auto_mode
    data = request.json
    device = data.get("device")
    command = data.get("command")
    if device == "auto":
        with state_lock:
            auto_mode = (command == "on")
            current_auto_mode = auto_mode
        logging.info("Modalità automatica impostata su: %s", current_auto_mode)
        return jsonify({"auto_mode": current_auto_mode})
    if device in DEVICES:
        ComandoPulsante(DEVICES[device], command)
        return jsonify({"ok": True})
    return jsonify({"error": "device sconosciuto"}), 400

@app.route("/set_thresholds", methods=["POST"])
def set_thresholds():
    global thresholds
    with state_lock:
        thresholds.update(request.json)
        current_thresholds = thresholds.copy()
    logging.info("Soglie aggiornate: %s", current_thresholds)
    return jsonify(current_thresholds)

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
            .status { padding: 5px 10px; border-radius: 6px; display:inline-block; }
            .on { background: #4caf50; color:white; }
            .off { background: #f44336; color:white; }
            .unreachable { background: #999; color:white; }
            .controls button { margin: 5px; padding: 10px 15px; border: none; border-radius: 6px; cursor: pointer; }
            .controls .on { background:#4caf50; }
            .controls .off { background:#f44336; }
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
                        if (!json.stati[dev]) {
                            continue;
                        }
                        el.className = "status " + (json.stati[dev].state || "unreachable");
                        el.innerText = json.stati[dev].label || "?";
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
