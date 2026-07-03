import time
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs"
CHAT_ID = "-1004321946575"

URL_VOLS = "https://www.nice.aeroport.fr/en/flights/arrivals"
PARIS = ZoneInfo("Europe/Paris")

vols_deja_annonces = set()
dernier_resume = None


def envoyer_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})


def recuperer_vols():
    html = requests.get(URL_VOLS, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")
    lignes = [x.strip() for x in soup.get_text("\n").split("\n") if x.strip()]

    vols = []
    for i, ligne in enumerate(lignes):
        if re.fullmatch(r"\d{1,2}:\d{2}", ligne):
            bloc = lignes[i:i+18]
            if len(bloc) < 6:
                continue

            heure_prevue = bloc[0]
            ville = bloc[1]

            terminal = None
            for x in bloc:
                if x in ["1", "2"]:
                    terminal = x
                    break

            statut = ""
            for x in bloc:
                if any(mot in x for mot in ["Arrived", "Expected", "Delayed", "Landing", "Cancelled"]):
                    statut = x
                    break

            codes = [x for x in bloc if re.fullmatch(r"[A-Z0-9]{2,3}\d{2,5}[A-Z]?", x)]
            numero = codes[0] if codes else "N/A"

            compagnie = "N/A"
            for x in bloc[2:]:
                if x not in codes and x not in ["1", "2"] and not any(m in x for m in ["Arrived", "Expected", "Delayed", "Landing"]):
                    if not re.fullmatch(r"\d{1,2}:\d{2}", x):
                        compagnie = x
                        break

            if terminal and statut:
                vols.append({
                    "heure": heure_prevue,
                    "ville": ville,
                    "numero": numero,
                    "compagnie": compagnie,
                    "terminal": terminal,
                    "statut": statut
                })
    return vols


def envoyer_alertes(vols):
    for vol in vols:
        cle = f"{vol['numero']}-{vol['statut']}"
        if cle in vols_deja_annonces:
            continue

        if "Arrived" in vol["statut"] or "Landing" in vol["statut"]:
            message = (
                "🛬 AVION ARRIVÉ / EN APPROCHE\n\n"
                f"✈️ {vol['numero']}\n"
                f"🏢 {vol['compagnie']}\n"
                f"🌍 Provenance : {vol['ville']}\n"
                f"📍 Terminal : {vol['terminal']}\n"
                f"🕒 Prévu : {vol['heure']}\n"
                f"📌 Statut : {vol['statut']}"
            )
            envoyer_telegram(message)
            vols_deja_annonces.add(cle)


def envoyer_resume(vols):
    maintenant = datetime.now(PARIS)
    t30 = maintenant + timedelta(minutes=30)
    t60 = maintenant + timedelta(minutes=60)

    dans_30 = []
    dans_60 = []

    for vol in vols:
        try:
            h, m = map(int, vol["heure"].split(":"))
            heure_vol = maintenant.replace(hour=h, minute=m, second=0, microsecond=0)
            if maintenant <= heure_vol <= t30:
                dans_30.append(vol)
            if maintenant <= heure_vol <= t60:
                dans_60.append(vol)
        except:
            pass

    t1_30 = sum(1 for v in dans_30 if v["terminal"] == "1")
    t2_30 = sum(1 for v in dans_30 if v["terminal"] == "2")

    message = (
        "✈️ EASYTAXI FLIGHT ALERT\n\n"
        "📊 Résumé des arrivées\n\n"
        f"⏱️ Dans les 30 min : {len(dans_30)} vols\n"
        f"Terminal 1 : {t1_30}\n"
        f"Terminal 2 : {t2_30}\n\n"
        f"🕐 Dans la prochaine heure : {len(dans_60)} vols\n\n"
    )

    for vol in dans_30[:8]:
        message += f"• {vol['heure']} - {vol['ville']} - {vol['numero']} - T{vol['terminal']} - {vol['statut']}\n"

    envoyer_telegram(message)


def boucle():
    global dernier_resume

    envoyer_telegram("✅ EasyTaxi Flight Alert est lancé 24h/24.")

    while True:
        try:
            vols = recuperer_vols()
            envoyer_alertes(vols)

            maintenant = datetime.now(PARIS)
            if dernier_resume is None or (maintenant - dernier_resume).seconds >= 1800:
                envoyer_resume(vols)
                dernier_resume = maintenant

        except Exception as e:
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}")

        time.sleep(300)


if __name__ == "__main__":
    boucle()
