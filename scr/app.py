from flask import Flask, render_template_string, request
from tuya_connector import TuyaOpenAPI

# Credenziali Tuya
ACCESS_ID = "xts98pchwtkrg3g94wap"
ACCESS_SECRET = "5b2536b117bc423ca6fd6876c5c2f16e"
API_ENDPOINT = "https://openapi.tuyaeu.com"
DEVICE_ID = "5676087234ab950d580a"

# Connessione all'API Tuya
openapi = TuyaOpenAPI(API_ENDPOINT, ACCESS_ID, ACCESS_SECRET)
openapi.connect()

# App Flask
app = Flask(__name__)

# HTML semplice con due pulsanti
HTML = """
<!doctype html>
<title>Controllo Stufa SmartLife</title>
<h2>Stufa SmartLife</h2>
<form method="post">
    <button name="action" value="on">🔥 Accendi</button>
    <button name="action" value="off">❄️ Spegni</button>
</form>
"""

@app.route("/", methods=["GET", "POST"])
def control():
    if request.method == "POST":
        action = request.form["action"]
        if action == "on":
            openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID}/commands", {
                "commands": [{"code": "switch_1", "value": True}]
            })
        elif action == "off":
            openapi.post(f"/v1.0/iot-03/devices/{DEVICE_ID}/commands", {
                "commands": [{"code": "switch_1", "value": False}]
            })
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
