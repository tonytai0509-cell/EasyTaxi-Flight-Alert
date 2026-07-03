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

# Optimisation quota AeroDataBox :
# 1 appel toutes les 15 minutes ≈ 5 952 unités/mois si l'appel vaut 2 unités.
FREQUENCE_API_SECONDES = 900
FREQUENCE_RESUME_SECONDES = 1800
RETARD_IMPORTANT_MINUTES = 20

vols_cache = []
derniere_maj_api = None
dernier_resume = None
vols_arrivee_deja_annonces = set()
retards_deja_annonces = set()
telegram_update_offset = None


# =========================
# TELEGRAM
# =========================

def envoyer_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        if not r.ok:
            print("Erreur Telegram:", r.text)
    except Exception as e:
        print("Exception Telegram:", e)


def recuperer_updates():
    global telegram_update_offset

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 1}
    if telegram_update_offset is not None:
        params["offset"] = telegram_update_offset

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        updates = data.get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        print("Erreur getUpdates:", e)
        return []


def ignorer_anciennes_commandes():
    global telegram_update_offset
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        r = requests.get(url, timeout=10)
        updates = r.json().get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print("Erreur init offset:", e)


# =========================
# OUTILS
# =========================

def maintenant():
    return datetime.now(PARIS)


def parse_iso_local(value):
    if not value:
        return None
    try:
        # Exemple AeroDataBox : 2026-07-03 17:25+02:00 ou 2026-07-03T17:25:00
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        try:
            return datetime.strptime(value[:16], "%Y-%m-%d %H:%M").replace(tzinfo=PARIS)
        except Exception:
            return None


def hhmm(dt):
    if not dt:
        return "N/A"
    return dt.astimezone(PARIS).strftime("%H:%M")


def badge_terminal(terminal):
    t = str(terminal or "").strip()
    if t == "1":
        return "🔵 <b>TERMINAL 1</b>"
    if t == "2":
        return "🟣 <b>TERMINAL 2</b>"
    return "⚪ <b>TERMINAL inconnu</b>"


def statut_fr(status):
    s = (status or "").lower()
    if "arriv" in s or "landed" in s:
        return "Atterri"
    if "approach" in s or "landing" in s:
        return "En approche"
    if "delay" in s:
        return "Retardé"
    if "cancel" in s:
        return "Annulé"
    if "expected" in s or "scheduled" in s:
        return "Prévu"
    return status or "Prévu"


def est_arrive(status):
    s = (status or "").lower()
    return "arriv" in s or "landed" in s


def est_en_approche(status):
    s = (status or "").lower()
    return "approach" in s or "landing" in s


def cle_arrivee(vol):
    return f"{vol['numero']}-{vol['prevu']}-{vol['terminal']}-{vol['provenance']}"


def cle_retard(vol):
    return f"{vol['numero']}-{vol['prevu']}-{vol['actuel']}-{vol['terminal']}"


# =========================
# AERODATABOX
# =========================

def recuperer_arrivees_aerodatabox():
    if not RAPIDAPI_KEY:
        raise Exception("RAPIDAPI_KEY manquante dans Railway > Variables")

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
        "withPrivate": "false"
    }

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST
    }

    r = requests.get(url, headers=headers, params=params, timeout=25)

    if not r.ok:
        raise Exception(f"AeroDataBox erreur {r.status_code}: {r.text[:300]}")

    data = r.json()
    arrivees = data.get("arrivals", [])

    vols = []

    for item in arrivees:
        arrival = item.get("arrival", {}) or {}
        departure = item.get("departure", {}) or {}
        airline = item.get("airline", {}) or {}

        numero = item.get("number") or item.get("callSign") or "N/A"
        compagnie = airline.get("name") or "N/A"

        dep_airport = departure.get("airport", {}) or {}
        provenance = dep_airport.get("municipalityName") or dep_airport.get("name") or dep_airport.get("iata") or "Inconnue"

        scheduled = arrival.get("scheduledTime", {}) or {}
        revised = arrival.get("revisedTime", {}) or {}
        actual = arrival.get("actualTime", {}) or {}
        predicted = arrival.get("predictedTime", {}) or {}

        dt_prevu = parse_iso_local(scheduled.get("local"))
        dt_actuel = (
            parse_iso_local(actual.get("local"))
            or parse_iso_local(revised.get("local"))
            or parse_iso_local(predicted.get("local"))
            or dt_prevu
        )

        status = item.get("status") or arrival.get("status") or "Expected"
        terminal = arrival.get("terminal") or ""

        retard = 0
        if dt_prevu and dt_actuel:
            retard = int((dt_actuel - dt_prevu).total_seconds() // 60)

        vols.append({
            "numero": numero,
            "compagnie": compagnie.upper(),
            "provenance": provenance.upper(),
            "terminal": str(terminal),
            "status": status,
            "status_fr": statut_fr(status),
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": hhmm(dt_prevu),
            "actuel": hhmm(dt_actuel),
            "retard": retard
        })

    vols.sort(key=lambda v: v["dt_actuel"] or v["dt_prevu"] or maintenant())
    return vols


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
# RESUME
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


def creer_resume(vols):
    d30 = vols_dans_minutes(vols, 30)
    d60 = vols_dans_minutes(vols, 60)

    t1_30 = sum(1 for v in d30 if v["terminal"] == "1")
    t2_30 = sum(1 for v in d30 if v["terminal"] == "2")
    t1_60 = sum(1 for v in d60 if v["terminal"] == "1")
    t2_60 = sum(1 for v in d60 if v["terminal"] == "2")

    retards = [v for v in vols if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive(v["status"])]

    message = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n\n"
        f"🕒 Mise à jour : {maintenant().strftime('%H:%M')}\n"
        f"🚖 Activité : <b>{niveau_affluence(len(d30))}</b>\n"
        f"⚠️ Retards importants : <b>{len(retards)}</b>\n\n"
        f"⏱️ <b>Dans les 30 min :</b> {len(d30)} vols\n"
        f"🔵 Terminal 1 : {t1_30}\n"
        f"🟣 Terminal 2 : {t2_30}\n\n"
        f"🕐 <b>Dans la prochaine heure :</b> {len(d60)} vols\n"
        f"🔵 Terminal 1 : {t1_60}\n"
        f"🟣 Terminal 2 : {t2_60}\n\n"
    )

    if d30:
        message += "🛬 <b>Prochains vols :</b>\n"
        for v in d30[:12]:
            message += (
                f"• {v['actuel']} - {v['provenance']} - "
                f"{v['compagnie']} - {badge_terminal(v['terminal'])} - {v['status_fr']}\n"
            )
    else:
        message += "Aucun vol prévu dans les 30 prochaines minutes."

    return message


# =========================
# ALERTES
# =========================

def initialiser_sans_spam(vols):
    for v in vols:
        if est_arrive(v["status"]) or est_en_approche(v["status"]):
            vols_arrivee_deja_annonces.add(cle_arrivee(v))
        if v["retard"] >= RETARD_IMPORTANT_MINUTES:
            retards_deja_annonces.add(cle_retard(v))


def envoyer_alertes_arrivees(vols):
    nb30 = len(vols_dans_minutes(vols, 30))

    for v in vols:
        if not (est_arrive(v["status"]) or est_en_approche(v["status"])):
            continue

        cle = cle_arrivee(v)
        if cle in vols_arrivee_deja_annonces:
            continue

        message = (
            "🚨 <b>NOUVELLE ARRIVÉE</b> 🚨\n\n"
            f"✈️ <b>{v['compagnie']}</b>\n"
            f"🌍 Provenance : <b>{v['provenance']}</b>\n"
            f"📍 {badge_terminal(v['terminal'])}\n"
            f"🕒 Heure prévue : {v['prevu']}\n"
            f"📌 Statut : {v['status_fr']} {v['actuel']}\n\n"
            f"🚖 Vols dans les 30 prochaines minutes : <b>{nb30}</b>"
        )

        envoyer_telegram(message)
        vols_arrivee_deja_annonces.add(cle)


def envoyer_alertes_retards(vols):
    for v in vols:
        if v["retard"] < RETARD_IMPORTANT_MINUTES:
            continue
        if est_arrive(v["status"]):
            continue

        cle = cle_retard(v)
        if cle in retards_deja_annonces:
            continue

        message = (
            "⚠️ <b>RETARD IMPORTANT</b> ⚠️\n\n"
            f"✈️ <b>{v['compagnie']}</b>\n"
            f"🌍 Provenance : <b>{v['provenance']}</b>\n"
            f"📍 {badge_terminal(v['terminal'])}\n"
            f"🕒 Heure prévue : {v['prevu']}\n"
            f"⏰ Nouvelle heure : {v['actuel']}\n"
            f"⌛ Retard : <b>{v['retard']} min</b>\n\n"
            "🚖 Les chauffeurs peuvent adapter leur position."
        )

        envoyer_telegram(message)
        retards_deja_annonces.add(cle)


# =========================
# COMMANDES
# =========================

def gerer_commandes():
    updates = recuperer_updates()

    for update in updates:
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat_id = str(message.get("chat", {}).get("id"))
        texte = (message.get("text") or "").strip().lower()

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        # IMPORTANT : /test et /vols utilisent le cache pour ne PAS consommer d'unités API.
        if texte.startswith("/test") or texte.startswith("/vols"):
            envoyer_telegram(creer_resume(vols_cache))

        elif texte.startswith("/quota"):
            age = "jamais"
            if derniere_maj_api:
                age = f"{int((maintenant() - derniere_maj_api).total_seconds() // 60)} min"
            envoyer_telegram(
                "📊 <b>Quota API</b>\n\n"
                "Réglage actuel : 1 appel API toutes les 15 minutes.\n"
                "Objectif : rester sous 6 000 unités/mois.\n\n"
                f"Dernier appel API : il y a {age}."
            )

        elif texte.startswith("/help"):
            envoyer_telegram(
                "🤖 <b>Commandes EasyTaxi Flight Alert</b>\n\n"
                "/test - Résumé actuel sans consommer d'API\n"
                "/vols - Prochains vols sans consommer d'API\n"
                "/quota - Infos quota API\n"
                "/help - Aide"
            )


# =========================
# BOUCLE PRINCIPALE
# =========================

def boucle_principale():
    global dernier_resume

    ignorer_anciennes_commandes()

    envoyer_telegram(
        "✅ <b>EasyTaxi Flight Alert V5 lancé</b>\n"
        "AeroDataBox activé.\n"
        "Optimisation quota : 1 appel API toutes les 15 minutes.\n\n"
        "Commandes : /test /vols /quota"
    )

    try:
        vols = mettre_a_jour_cache_si_besoin(force=True)
        initialiser_sans_spam(vols)
        envoyer_telegram(creer_resume(vols))
        dernier_resume = maintenant()
    except Exception as e:
        envoyer_telegram(f"⚠️ Erreur démarrage AeroDataBox : {e}")

    while True:
        try:
            vols = mettre_a_jour_cache_si_besoin(force=False)

            gerer_commandes()
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
