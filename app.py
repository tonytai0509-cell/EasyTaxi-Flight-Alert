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

# Quota : 1 appel toutes les 15 min.
FREQUENCE_API_SECONDES = 900
FREQUENCE_RESUME_SECONDES = 1800
RETARD_IMPORTANT_MINUTES = 20

vols_cache = []
derniere_maj_api = None
dernier_resume = None

vols_arrivee_deja_annonces = set()
retards_deja_annonces = set()


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


def statut_fr(status):
    s = (status or "").lower()
    if "arriv" in s or "landed" in s:
        return "Atterri"
    if "approach" in s or "landing" in s:
        return "Approche"
    if "delay" in s:
        return "Retardé"
    if "cancel" in s:
        return "Annulé"
    return "Prévu"


def est_arrive_ou_approche(status):
    s = (status or "").lower()
    return "arriv" in s or "landed" in s or "approach" in s or "landing" in s


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
    }
    return remplacements.get(nom, nom)


def cle_arrivee(v):
    return f"{v['numero']}-{v['prevu']}-{v['actuel']}-{v['terminal']}-{v['provenance']}"


def cle_retard(v):
    return f"{v['numero']}-{v['prevu']}-{v['actuel']}-{v['terminal']}"


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
        dt_actuel = (
            parse_iso(actual.get("local"))
            or parse_iso(revised.get("local"))
            or parse_iso(predicted.get("local"))
            or dt_prevu
        )

        status = item.get("status") or "Expected"
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
            "status_fr": statut_fr(status),
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": hhmm(dt_prevu),
            "actuel": hhmm(dt_actuel),
            "retard": retard,
        })

    # Supprime les doublons de codeshare : même ville, même heure, même terminal.
    uniques = {}
    for v in vols:
        cle = f"{v['actuel']}-{v['provenance']}-{v['terminal']}"
        if cle not in uniques:
            uniques[cle] = v
        else:
            # on garde le nom de compagnie le plus court/clair
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
# RÉSUMÉ UNIQUE ET COMPACT
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
    statut = v["status_fr"]
    if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(v["status"]):
        statut = f"+{v['retard']}min"
    return f"• {v['actuel']} {v['provenance']} - {v['compagnie']} - {statut}"


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

    retards = [
        v for v in vols
        if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(v["status"])
    ]

    msg = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n"
        f"🕒 {maintenant().strftime('%H:%M')} | 🚖 {niveau_affluence(len(d30))} | ⚠️ {len(retards)}\n"
        f"⏱️ 30min : <b>{len(d30)}</b> (🔵{len(t1_30)} / 🟣{len(t2_30)})\n"
        f"🕐 1h : <b>{len(d60)}</b> (🔵{len(t1_60)} / 🟣{len(t2_60)})\n\n"
        "🛬 <b>Prochains 30min</b>\n"
    )

    msg += bloc_terminal("🔵 T1", t1_30)
    msg += bloc_terminal("🟣 T2", t2_30)

    return msg.strip()


# =========================
# ALERTES
# =========================

def initialiser_sans_spam(vols):
    for v in vols:
        if est_arrive_ou_approche(v["status"]):
            vols_arrivee_deja_annonces.add(cle_arrivee(v))
        if v["retard"] >= RETARD_IMPORTANT_MINUTES:
            retards_deja_annonces.add(cle_retard(v))


def envoyer_alertes_arrivees(vols):
    nb30 = len(vols_dans_minutes(vols, 30))

    for v in vols:
        if not est_arrive_ou_approche(v["status"]):
            continue

        cle = cle_arrivee(v)
        if cle in vols_arrivee_deja_annonces:
            continue

        message = (
            "🚨 <b>NOUVELLE ARRIVÉE</b> 🚨\n"
            f"✈️ <b>{v['compagnie']}</b> | {emoji_terminal(v['terminal'])}\n"
            f"🌍 {v['provenance']}\n"
            f"🕒 {v['prevu']} → {v['status_fr']} {v['actuel']}\n"
            f"🚖 30min : <b>{nb30}</b> vols"
        )

        envoyer_telegram(message)
        vols_arrivee_deja_annonces.add(cle)


def envoyer_alertes_retards(vols):
    for v in vols:
        if v["retard"] < RETARD_IMPORTANT_MINUTES:
            continue
        if est_arrive_ou_approche(v["status"]):
            continue

        cle = cle_retard(v)
        if cle in retards_deja_annonces:
            continue

        message = (
            "⚠️ <b>RETARD IMPORTANT</b> ⚠️\n"
            f"✈️ <b>{v['compagnie']}</b> | {emoji_terminal(v['terminal'])}\n"
            f"🌍 {v['provenance']}\n"
            f"🕒 {v['prevu']} → {v['actuel']} (<b>+{v['retard']}min</b>)"
        )

        envoyer_telegram(message)
        retards_deja_annonces.add(cle)


# =========================
# BOUCLE PRINCIPALE
# =========================

def boucle_principale():
    global dernier_resume

    envoyer_telegram("✅ <b>EasyTaxi Flight Alert V7 lancé</b>\nMode compact définitif.")

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

            envoyer_alertes_arrivees(vols)
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
