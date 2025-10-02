###### versione 2.0 , interroga inverter, usa soglie per attivare stufe, pilota direttamente STUFAG e StufaP collegata a Tuya (smartlife) 
from flask import Flask, jsonify, render_template_string, request
from tuya_connector import TuyaOpenAPI

import requests
import threading
import time
import logging
import datetime


# Configurazione x Inverter Fronius Gen24Plus
INVERTER_IP = "192.168.1.100"

# Credenziali Tuya e dati dispositivi
API_ENDPOINT = "https://openapi.tuyaeu.com"
ACCESS_ID = "tuo access id tuya"
ACCESS_SECRET = "tuo access secret tuya"

DEVICE_ID = "tuo devicess id 1 tuya"
DEVICE_ID2 = "tuo devicess id 2 tuya"
DEVICES = {
    "StufaP": "tuo devicess id 1 tuya",
    "StufaG": "tuo devicess id 2 tuya"}


# Connessione all'API Tuya
openapi = TuyaOpenAPI(API_ENDPOINT, ACCESS_ID, ACCESS_SECRET)
openapi.connect()

app = Flask(__name__)

device_states = {"StufaP": "unknown", "StufaG": "unknown"}
auto_mode = True
thresholds = {"X": 500, "Y": 1000, "Z": 800, "D": 200} # soglie di accensio spegnimento dispositivi Tuya

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# --- Funzioni Gestione prese stufe ---
def ComandoPulsante(device_id, command):
    try:
        if device_id==DEVICES["StufaG"]: ##if response.status_code == 200:

           if command=="on":
              openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID}/commands", {"commands": [{"code": "switch_1", "value": True}]})
           else:
              openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID}/commands", {"commands": [{"code": "switch_1", "value": False}]})

        if device_id==DEVICES["StufaP"]: ##if response.status_code == 200:
           if command=="on":
              openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID2}/commands", {"commands": [{"code": "switch_1", "value": True}]})
           else:
              openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID2}/commands", {"commands": [{"code": "switch_1", "value": False}]})

    except Exception as e:
        print("⚠️ Codice 'switch_1' non trovato, dispositivo forse non rintracciabile")

def StatoDispositivi(device_idx):

# Connessione all'API Tuya
#    openapi = TuyaOpenAPI(API_ENDPOINT, ACCESS_ID, ACCESS_SECRET)
#    openapi.connect()
    status = openapi.get(f"/v1.0/iot-03/devices/{device_idx}/status")

# Analisi dello stato di StufaP e Stufa G
    for item in status.get("result", []):
        if device_idx==DEVICE_ID2:
            if item["value"] is True:
                return "🔥 StufaP è ACCESA" #print("🔥 StufaP è ACCESA")
            elif item["value"] is False:
                return "❄️ StufaP è SPENTA" #print("❄️ StufaP è SPENTA")
            else:
                return "⚠️ Stato Stufa P non riconosciuto"  #print("⚠️ Stato non riconosciuto")
            break
        elif device_idx==DEVICE_ID:
            if item["value"] is True:
                return "🔥 StufaG è ACCESA"  #print("🔥 StufaG è ACCESA")
            elif item["value"] is False:
                return "❄️ StufaG è SPENTA"  #print("❄️ StufaG è SPENTA")
            else:
                return "⚠️ Stato Stufa G non riconosciuto"  #print("⚠️ Stato non riconosciuto")
            break
            
    else:
         return "⚠️ Stato Stufa G non riconosciuto"  #print("⚠️ Stato non riconosciuto")
#       print("⚠️ Codice 'switch_1' non trovato, dispositivo forse non rintracciabile")


# --- Automazione --- quando va in automatico, ogno 30 secondi dentro la fascia oraria stabilita,
# in base alle soglie di automazione gestisce le due stufe/prese

def automazione_loop():

    start_time = datetime.time(11, 0)
    end_time = datetime.time(17, 30)
    while True:
        now = datetime.datetime.now().time()
        if start_time <= now <= end_time and auto_mode:
            try:
                url = f"http://{INVERTER_IP}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
                r = requests.get(url, timeout=5)
                data = r.json()
                site = data["Body"]["Data"]["Site"]
                p_grid = site["P_Grid"] * -1

                # Accensione
                if p_grid > thresholds["Y"]:
                    ComandoPulsante(DEVICES["StufaG"], "on")
                    ComandoPulsante(DEVICES["StufaP"], "on")
                elif p_grid > thresholds["X"]:
                    ComandoPulsante(DEVICES["StufaG"], "off")

                # Spegnimento
                if p_grid < thresholds["Z"]:
                    ComandoPulsante(DEVICES["StufaG"], "off")

                if p_grid < thresholds["D"]:
                    logging.info(f"Automazione eseguita con rete: {p_grid} W")
            except Exception as e:
                logging.error(f"Errore automazione: {e}")
        time.sleep(30)

threading.Thread(target=automazione_loop, daemon=True).start()

# --- API --- preleva i dati di produzione, consumo, immissione in rete, livello di SOC e segnala lo stato dei dispositivi "acceso,spento"
@app.route("/data")
def get_data():
    url = f"http://{INVERTER_IP}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        site = data["Body"]["Data"]["Site"]
        inverter = data["Body"]["Data"]["Inverters"]["1"]
        p_grid = site["P_Grid"] * -1
        resp = {
            "produzione": site["P_PV"],
            "consumo": site["P_Load"],
            "rete": p_grid,
            "soc": inverter.get("SOC", 0),
            "stati": {d: StatoDispositivi(DEVICES[d]) for d in DEVICES},
            "auto_mode": auto_mode,
            "thresholds": thresholds
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
        auto_mode = (command == "on")
        logging.info(f"Modalità automatica impostata su: {auto_mode}")
        return jsonify({"auto_mode": auto_mode})
    if device in DEVICES:
        ComandoPulsante(DEVICES[device], command)
        return jsonify({"ok": True})
    return jsonify({"error": "device sconosciuto"}), 400

@app.route("/set_thresholds", methods=["POST"])
def set_thresholds():
    global thresholds
    thresholds.update(request.json)
    logging.info(f"Soglie aggiornate: {thresholds}")
    return jsonify(thresholds)

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
                        el.className = "status " + json.stati[dev];
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
