# Fronius-Tuya-Manager

Questo progetto permette di monitorare un inverter **Fronius GEN24 Plus** e automatizzare dispositivi **Tuya (SmartLife)** in base alla produzione fotovoltaica.

## 🚀 Funzionalità
- Lettura dati dall’inverter Fronius (produzione, consumo, SOC batteria).
- Controllo manuale di dispositivi Tuya (ON/OFF).
- Modalità automatica basata su soglie configurabili.
- Polling ottimizzato per esecuzione continua su **Termux**.
- Dashboard web locale con Flask.

## ⚙️ Ottimizzazioni introdotte
- Sessione HTTP persistente verso Fronius (meno overhead su CPU/rete).
- Cache stato dispositivi Tuya per evitare chiamate inutili ad ogni refresh UI.
- Invio comandi Tuya solo quando cambia realmente lo stato (meno traffico API).
- Parametri principali configurabili via variabili ambiente.

## 🧩 Variabili ambiente principali
- `INVERTER_IP` (default: `192.168.1.100`)
- `TUYA_API_ENDPOINT` (default: `https://openapi.tuyaeu.com`)
- `TUYA_ACCESS_ID`
- `TUYA_ACCESS_SECRET`
- `TUYA_DEVICE_ID_STUFAG`
- `TUYA_DEVICE_ID_STUFAP`
- `TUYA_DEVICE_ID_TERMOMETRO` (opzionale, per monitorare temperatura/umidità da un sensore Tuya)
- `POLL_SECONDS` (default: `20`)
- `STATUS_REFRESH_SECONDS` (default: `60`)
- `AUTO_START_HOUR`, `AUTO_START_MINUTE`
- `AUTO_END_HOUR`, `AUTO_END_MINUTE`

## ▶️ Avvio rapido su Termux
```bash
pkg update -y
pkg install -y python
python -m venv .venv
source .venv/bin/activate
pip install flask requests tuya-connector-python
python scr/app.py
```

Apri poi dal browser in LAN: `http://<ip-termux>:5000`

## 🌐 Note operative
Il software è pensato per uso **solo rete locale**. In scenari LAN trusted, queste ottimizzazioni privilegiano semplicità, resilienza e consumi ridotti.
