import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =========================
# CONFIGURATION
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004321946575")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "c43e14c814msh221d76b3577077ap15a88ajsna897fda6a4ef")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "aerodatabox.p.rapidapi.com")

AEROPORT_IATA = "NCE"
PARIS = ZoneInfo("Europe/Paris")

# 1 appel API toutes les 15 minutes pour rester proche du quota 6000/mois.
FREQUENCE_API_SECONDES = 900
FREQUENCE_RESUME_SECONDES = 1800
RETARD_IMPORTANT_MINUTES = 20

vols_cache = []
derniere_maj_api = None
dernier_resume = None

approches_deja_annoncees = set()
poses_deja_annonces = set()
retards_deja_annonces = {}


# =========================
# OUTILS
# =========================

def maintenant():
    return datetime.now(PARIS)


def parse_iso(value):
    if not value:
        return None
    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def hhmm(dt):
    if not dt:
        return "N/A"
    return dt.astimezone(PARIS).strftime("%H:%M")


def emoji_terminal(terminal):
    terminal = str(terminal or "").strip()
    if terminal == "1":
        return "🔵T1"
    if terminal == "2":
        return "🟣T2"
    return "⚪T?"


def label_terminal(terminal):
    terminal = str(terminal or "").strip()
    if terminal == "1":
        return "Terminal 1"
    if terminal == "2":
        return "Terminal 2"
    return "Terminal ?"


def nettoyer_nom(nom):
    nom = (nom or "").upper().strip()
    remplacements = {
        "AJACCIO/NAPOLÉON BONAPARTE": "AJACCIO",
        "BASTIA/PORETTA": "BASTIA",
        "BIARRITZ/ANGLET/BAYONNE": "BIARRITZ",
        "PARIS CHARLES DE GAULLE": "PARIS CDG",
        "PARIS ORLY": "PARIS ORLY",
        "LONDON": "LONDRES",
        "WARSAW": "VARSOVIE",
        "GENEVA": "GENÈVE",
        "COPENHAGEN": "COPENHAGUE",
    }
    return remplacements.get(nom, nom)


def nettoyer_compagnie(nom):
    nom = (nom or "").upper().strip()
    remplacements = {
        "NORWEGIAN AIR SWEDEN": "NORWEGIAN",
        "NORWEGIAN AIR SHUTTLE": "NORWEGIAN",
        "TRANSAVIA FRANCE": "TRANSAVIA",
        "BRITISH AIRWAYS": "BRITISH",
        "ROYAL AIR MAROC": "RAM",
        "SCANDINAVIAN AIRLINES": "SAS",
        "EASYJET EUROPE": "EASYJET",
    }
    return remplacements.get(nom, nom)


def est_pose(status):
    s = (status or "").lower()
    return "arriv" in s or "landed" in s


def est_approche(status):
    s = (status or "").lower()
    return "approach" in s or "landing" in s or "final" in s


def est_arrive_ou_approche(status):
    return est_pose(status) or est_approche(status)


def statut_lisible(v):
    status = (v.get("status") or "").lower()
    retard = v.get("retard", 0)

    if est_pose(status):
        return f"✅ Posé {v['actuel']}"

    if est_approche(status):
        return "🛬 Approche"

    if "cancel" in status:
        return "❌ Annulé"

    if "delay" in status or retard >= RETARD_IMPORTANT_MINUTES:
        return f"⏰ +{retard}min"

    if retard >= 10:
        return f"⏰ +{retard}min"

    return "🟢 Prévu"


def heure_lisible(v):
    if v["prevu"] != "N/A" and v["actuel"] != "N/A" and v["prevu"] != v["actuel"]:
        return f"{v['prevu']}→{v['actuel']}"
    return v["actuel"] if v["actuel"] != "N/A" else v["prevu"]


def cle_vol(v):
    return f"{v['numero']}-{v['provenance']}-{v['terminal']}-{v['prevu']}"


def sortie_passagers(v):
    """
    Estimation simple taxi :
    T1 : premiers passagers environ 10-18 min après posé.
    T2 : premiers passagers environ 12-22 min après posé.
    """
    if not v.get("dt_actuel"):
        return "Sortie passagers : bientôt"

    base = v["dt_actuel"].astimezone(PARIS)
    if str(v.get("terminal")) == "1":
        debut = base + timedelta(minutes=10)
        fin = base + timedelta(minutes=18)
    else:
        debut = base + timedelta(minutes=12)
        fin = base + timedelta(minutes=22)

    return f"🚖 Sortie clients estimée : {debut.strftime('%H:%M')} - {fin.strftime('%H:%M')}"


def envoyer_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not r.ok:
            print("Erreur Telegram:", r.text)
    except Exception as e:
        print("Erreur Telegram:", e)


# =========================
# AERODATABOX
# =========================

def recuperer_arrivees_aerodatabox():
    if not RAPIDAPI_KEY or RAPIDAPI_KEY == "COLLE_TA_CLE_RAPIDAPI_ICI":
        raise Exception("Clé RapidAPI absente")

    debut = maintenant() - timedelta(minutes=15)
    fin = maintenant() + timedelta(hours=2)

    debut_txt = debut.strftime("%Y-%m-%dT%H:%M")
    fin_txt = fin.strftime("%Y-%m-%dT%H:%M")

    url = f"https://{RAPIDAPI_HOST}/flights/airports/iata/{AEROPORT_IATA}/{debut_txt}/{fin_txt}"

    params = {
        "withLeg": "true",
        "direction": "Arrival",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "false",
        "withPrivate": "false",
    }

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
    }

    r = requests.get(url, headers=headers, params=params, timeout=25)
    if not r.ok:
        raise Exception(f"AeroDataBox {r.status_code}: {r.text[:250]}")

    data = r.json()
    arrivees = data.get("arrivals", [])
    vols = []

    for item in arrivees:
        arrival = item.get("arrival", {}) or {}
        departure = item.get("departure", {}) or {}
        airline = item.get("airline", {}) or {}

        numero = item.get("number") or item.get("callSign") or "N/A"
        compagnie = nettoyer_compagnie(airline.get("name") or "N/A")

        dep_airport = departure.get("airport", {}) or {}
        provenance = nettoyer_nom(
            dep_airport.get("municipalityName")
            or dep_airport.get("name")
            or dep_airport.get("iata")
            or "INCONNUE"
        )

        scheduled = arrival.get("scheduledTime", {}) or {}
        revised = arrival.get("revisedTime", {}) or {}
        actual = arrival.get("actualTime", {}) or {}
        predicted = arrival.get("predictedTime", {}) or {}

        dt_prevu = parse_iso(scheduled.get("local"))
        dt_revise = parse_iso(revised.get("local"))
        dt_actual = parse_iso(actual.get("local"))
        dt_predict = parse_iso(predicted.get("local"))

        dt_actuel = dt_actual or dt_revise or dt_predict or dt_prevu

        status = item.get("status") or arrival.get("status") or "Expected"
        terminal = str(arrival.get("terminal") or "")

        retard = 0
        if dt_prevu and dt_actuel:
            retard = int((dt_actuel - dt_prevu).total_seconds() // 60)

        vols.append({
            "numero": numero,
            "compagnie": compagnie,
            "provenance": provenance,
            "terminal": terminal,
            "status": status,
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": hhmm(dt_prevu),
            "actuel": hhmm(dt_actuel),
            "retard": retard,
        })

    # Suppression doublons codeshare : même ville, même heure, même terminal.
    uniques = {}
    for v in vols:
        cle = f"{v['actuel']}-{v['provenance']}-{v['terminal']}"
        if cle not in uniques:
            uniques[cle] = v
        else:
            if len(v["compagnie"]) < len(uniques[cle]["compagnie"]):
                uniques[cle] = v

    resultat = list(uniques.values())
    resultat.sort(key=lambda v: (v["dt_actuel"] or v["dt_prevu"] or maintenant(), v["terminal"]))
    return resultat


def mettre_a_jour_cache_si_besoin(force=False):
    global vols_cache, derniere_maj_api

    if not force and derniere_maj_api is not None:
        age = (maintenant() - derniere_maj_api).total_seconds()
        if age < FREQUENCE_API_SECONDES:
            return vols_cache

    vols_cache = recuperer_arrivees_aerodatabox()
    derniere_maj_api = maintenant()
    return vols_cache


# =========================
# RÉSUMÉ
# =========================

def vols_dans_minutes(vols, minutes):
    now = maintenant()
    limite = now + timedelta(minutes=minutes)
    return [
        v for v in vols
        if v["dt_actuel"] and now <= v["dt_actuel"].astimezone(PARIS) <= limite
    ]


def niveau_affluence(nb30):
    if nb30 >= 8:
        return "🔴 Forte"
    if nb30 >= 4:
        return "🟠 Moyenne"
    return "🟢 Calme"


def ligne_vol(v):
    return f"• {heure_lisible(v)} {v['provenance']} - {v['compagnie']} - {statut_lisible(v)}"


def bloc_terminal(titre, vols):
    if not vols:
        return f"{titre} : 0\n"
    lignes = [f"{titre} : {len(vols)}"]
    for v in vols:
        lignes.append(ligne_vol(v))
    return "\n".join(lignes) + "\n"


def creer_resume(vols):
    d30 = vols_dans_minutes(vols, 30)
    d60 = vols_dans_minutes(vols, 60)

    t1_30 = [v for v in d30 if v["terminal"] == "1"]
    t2_30 = [v for v in d30 if v["terminal"] == "2"]

    t1_60 = [v for v in d60 if v["terminal"] == "1"]
    t2_60 = [v for v in d60 if v["terminal"] == "2"]

    approches = [v for v in d30 if est_approche(v["status"])]
    poses = [v for v in d30 if est_pose(v["status"])]

    retards = [
        v for v in vols
        if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(v["status"])
    ]

    msg = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n"
        f"🕒 {maintenant().strftime('%H:%M')} | 🚖 {niveau_affluence(len(d30))} | ⚠️ {len(retards)}\n"
        f"🛬 Approche : {len(approches)} | ✅ Posés : {len(poses)}\n"
        f"⏱️ 30min : <b>{len(d30)}</b> (🔵{len(t1_30)} / 🟣{len(t2_30)})\n"
        f"🕐 1h : <b>{len(d60)}</b> (🔵{len(t1_60)} / 🟣{len(t2_60)})\n\n"
        "🛬 <b>Prochains 30min</b>\n"
    )

    msg += bloc_terminal("🔵 T1", t1_30)
    msg += bloc_terminal("🟣 T2", t2_30)

    return msg.strip()


# =========================
# ALERTES PRO
# =========================

def initialiser_sans_spam(vols):
    # Au démarrage, on ne spam pas les vols déjà en approche/posés.
    for v in vols:
        cle = cle_vol(v)
        if est_approche(v["status"]):
            approches_deja_annoncees.add(cle)
        if est_pose(v["status"]):
            poses_deja_annonces.add(cle)
        if v["retard"] >= RETARD_IMPORTANT_MINUTES:
            retards_deja_annonces[cle] = v["retard"]


def envoyer_alertes_approche(vols):
    for v in vols:
        if not est_approche(v["status"]):
            continue

        cle = cle_vol(v)
        if cle in approches_deja_annoncees:
            continue

        message = (
            "🛬 <b>EN APPROCHE</b>\n\n"
            f"🌍 <b>{v['provenance']}</b>\n"
            f"✈️ {v['compagnie']}\n"
            f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
            f"🕒 {heure_lisible(v)}"
        )

        envoyer_telegram(message)
        approches_deja_annoncees.add(cle)


def envoyer_alertes_pose(vols):
    for v in vols:
        if not est_pose(v["status"]):
            continue

        cle = cle_vol(v)
        if cle in poses_deja_annonces:
            continue

        message = (
            "✅ <b>POSÉ</b>\n\n"
            f"🌍 <b>{v['provenance']}</b>\n"
            f"✈️ {v['compagnie']}\n"
            f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
            f"🕒 {v['actuel']}\n"
            f"{sortie_passagers(v)}"
        )

        envoyer_telegram(message)
        poses_deja_annonces.add(cle)


def envoyer_alertes_retards(vols):
    for v in vols:
        if v["retard"] < RETARD_IMPORTANT_MINUTES:
            continue
        if est_arrive_ou_approche(v["status"]):
            continue

        cle = cle_vol(v)
        ancien_retard = retards_deja_annonces.get(cle)

        # Évite le spam : on renvoie seulement si le retard augmente d'au moins 10 min.
        if ancien_retard is not None and v["retard"] < ancien_retard + 10:
            continue

        message = (
            "⏰ <b>RETARD</b>\n\n"
            f"🌍 <b>{v['provenance']}</b>\n"
            f"✈️ {v['compagnie']}\n"
            f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
            f"🕒 {heure_lisible(v)} (<b>+{v['retard']}min</b>)"
        )

        envoyer_telegram(message)
        retards_deja_annonces[cle] = v["retard"]


# =========================
# BOUCLE PRINCIPALE
# =========================

def boucle_principale():
    global dernier_resume

    envoyer_telegram(
        "✅ <b>EasyTaxi Flight Alert V9 Pro lancé</b>\n"
        "Alertes : Approche / Posé / Retard + estimation sortie clients."
    )

    try:
        vols = mettre_a_jour_cache_si_besoin(force=True)
        initialiser_sans_spam(vols)
        envoyer_telegram(creer_resume(vols))
        dernier_resume = maintenant()
    except Exception as e:
        envoyer_telegram(f"⚠️ Erreur démarrage : {e}")

    while True:
        try:
            vols = mettre_a_jour_cache_si_besoin(force=False)

            envoyer_alertes_approche(vols)
            envoyer_alertes_pose(vols)
            envoyer_alertes_retards(vols)

            now = maintenant()
            if dernier_resume is None or (now - dernier_resume).total_seconds() >= FREQUENCE_RESUME_SECONDES:
                envoyer_telegram(creer_resume(vols))
                dernier_resume = now

        except Exception as e:
            print("Erreur boucle:", e)
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}")

        time.sleep(10)


if __name__ == "__main__":
    boucle_principale()
