# Fronius-Tuya-Manager

Questo progetto permette di monitorare un inverter **Fronius GEN24 Plus** e di automatizzare l’accensione di dispositivi **Tuya (SmartLife)**, come ad esempio stufe elettriche, in base alla produzione fotovoltaica.

## 🚀 Funzionalità
- Lettura dati dall’inverter Fronius (produzione, consumo, SOC batteria).
- Controllo manuale di dispositivi Tuya (ON/OFF).
- Modalità automatica basata su soglie configurabili di produzione elettrica immessa in rete (dopo aver caricato la batteria).
le 4 soglie X,Y,Z,D rappresentano i livelli di produzione per accendere i dispositivi (stufe ad esempio), mentre Z e D per spegnere i dispositivi
- Interfaccia web semplice in Flask.
