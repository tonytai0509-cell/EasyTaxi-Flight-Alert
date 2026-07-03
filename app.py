import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =========================
# CONFIGURATION
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004321946575")

URL_ARRIVEES = "https://www.nice.aeroport.fr/en/flights/arrivals"

FREQUENCE_VERIFICATION_SECONDES = 300      # 5 minutes
FREQUENCE_RESUME_SECONDES = 1800           # 30 minutes
RETARD_IMPORTANT_MINUTES = 20              # alerte retard à partir de 20 min

PARIS = ZoneInfo("Europe/Paris")

vols_deja_annonces = set()
retards_deja_annonces = set()
dernier_resume = None
telegram_update_offset = None


# =========================
# TELEGRAM
# =========================

def envoyer_telegram(message):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "COLLE_TON_TOKEN_ICI":
        print("ERREUR: TELEGRAM_TOKEN manquant.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        reponse = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
        if not reponse.ok:
            print("Erreur Telegram:", reponse.text)
    except Exception as e:
        print("Exception Telegram:", e)


def recuperer_updates():
    global telegram_update_offset

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 1}

    if telegram_update_offset is not None:
        params["offset"] = telegram_update_offset

    try:
        reponse = requests.get(url, params=params, timeout=10)
        data = reponse.json()
        if not data.get("ok"):
            return []

        updates = data.get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1

        return updates
    except Exception as e:
        print("Erreur getUpdates:", e)
        return []


def ignorer_anciennes_commandes():
    global telegram_update_offset

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        reponse = requests.get(url, timeout=10)
        data = reponse.json()
        updates = data.get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1
    except Exception as e:
        print("Erreur initialisation offset:", e)


def gerer_commandes(vols):
    updates = recuperer_updates()

    for update in updates:
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        texte = (message.get("text") or "").strip().lower()

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        if texte.startswith("/test") or texte.startswith("/vols"):
            envoyer_telegram(creer_resume(vols))

        elif texte.startswith("/help"):
            envoyer_telegram(
                "🤖 <b>Commandes EasyTaxi Flight Alert</b>\n\n"
                "/test - Affiche le résumé actuel\n"
                "/vols - Affiche les prochains vols\n"
                "/help - Affiche l'aide"
            )


# =========================
# OUTILS
# =========================

def maintenant_paris():
    return datetime.now(PARIS)


def nettoyer(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def statut_est_arrive(statut):
    s = statut.lower()
    mots = ["arrived", "landing", "landed", "atterri", "arrivé"]
    return any(mot in s for mot in mots)


def statut_est_retarde(statut):
    s = statut.lower()
    return "delayed" in s or "retard" in s


def traduire_statut(statut):
    s = statut.lower()

    heure = extraire_derniere_heure(statut)

    if "arrived" in s or "landed" in s:
        return f"Atterri {heure}" if heure else "Atterri"

    if "landing" in s:
        return f"En approche {heure}" if heure else "En approche"

    if "delayed" in s:
        return f"Retardé {heure}" if heure else "Retardé"

    if "expected" in s:
        return f"Prévu {heure}" if heure else "Prévu"

    if "cancel" in s:
        return "Annulé"

    return statut


def extraire_derniere_heure(texte):
    heures = re.findall(r"\b\d{1,2}:\d{2}\b", texte or "")
    return heures[-1] if heures else None


def convertir_heure_du_jour(heure_txt):
    h, m = map(int, heure_txt.split(":"))
    now = maintenant_paris()
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def minutes_entre(heure_depart_txt, heure_arrivee_txt):
    try:
        h1 = convertir_heure_du_jour(heure_depart_txt)
        h2 = convertir_heure_du_jour(heure_arrivee_txt)
        return int((h2 - h1).total_seconds() // 60)
    except Exception:
        return None


def badge_terminal(terminal):
    if str(terminal) == "1":
        return "🔵 <b>TERMINAL 1</b>"
    if str(terminal) == "2":
        return "🟣 <b>TERMINAL 2</b>"
    return f"Terminal {terminal}"


# =========================
# RECUPERATION DES VOLS
# =========================

def recuperer_lignes_page():
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/4.0"}
    html = requests.get(URL_ARRIVEES, headers=headers, timeout=25).text
    soup = BeautifulSoup(html, "html.parser")

    texte = soup.get_text("\n")
    lignes = [nettoyer(x) for x in texte.split("\n")]
    lignes = [x for x in lignes if x]
    return lignes


def extraire_vols_depuis_lignes(lignes):
    vols = []

    for i, ligne in enumerate(lignes):
        if not re.fullmatch(r"\d{1,2}:\d{2}", ligne):
            continue

        bloc = lignes[i:i+25]
        if len(bloc) < 5:
            continue

        heure_prevue = bloc[0]
        ville = bloc[1] if len(bloc) > 1 else "Inconnue"

        terminal = None
        for x in bloc:
            if x in ["1", "2"]:
                terminal = x
                break

        numeros = []
        for x in bloc:
            if re.fullmatch(r"[A-Z0-9]{2,3}\s?\d{2,5}[A-Z]?", x):
                numeros.append(x.replace(" ", ""))

        numero_vol = numeros[0] if numeros else "N/A"

        statut = ""
        mots_statut = ["Arrived", "Expected", "Delayed", "Landing", "Cancelled", "On time"]
        for x in bloc:
            if any(mot.lower() in x.lower() for mot in mots_statut):
                statut = x
                break

        compagnie = "N/A"
        elements_interdits = set([heure_prevue, ville, terminal or "", numero_vol])
        for x in bloc[2:]:
            if x in elements_interdits:
                continue
            if x in ["1", "2"]:
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}", x):
                continue
            if re.fullmatch(r"[A-Z0-9]{2,3}\s?\d{2,5}[A-Z]?", x):
                continue
            if any(mot.lower() in x.lower() for mot in mots_statut):
                continue
            if len(x) >= 3:
                compagnie = x
                break

        heure_reelle = extraire_derniere_heure(statut)

        if terminal in ["1", "2"] and statut:
            vols.append({
                "heure_prevue": heure_prevue,
                "heure_reelle": heure_reelle,
                "ville": ville.upper(),
                "numero_vol": numero_vol,
                "compagnie": compagnie.upper(),
                "terminal": terminal,
                "statut": statut,
                "statut_fr": traduire_statut(statut),
            })

    uniques = {}
    for vol in vols:
        cle = f"{vol['numero_vol']}-{vol['heure_prevue']}-{vol['terminal']}"
        uniques[cle] = vol

    return list(uniques.values())


def recuperer_vols():
    lignes = recuperer_lignes_page()
    return extraire_vols_depuis_lignes(lignes)


# =========================
# RESUMES
# =========================

def vols_dans_delai(vols, minutes):
    now = maintenant_paris()
    limite = now + timedelta(minutes=minutes)

    resultats = []
    for vol in vols:
        try:
            heure_vol = convertir_heure_du_jour(vol["heure_prevue"])
            if now <= heure_vol <= limite:
                resultats.append(vol)
        except Exception:
            pass

    return resultats


def retards_importants(vols):
    resultats = []

    for vol in vols:
        if not statut_est_retarde(vol["statut"]):
            continue

        nouvelle_heure = vol.get("heure_reelle")
        if not nouvelle_heure:
            continue

        retard = minutes_entre(vol["heure_prevue"], nouvelle_heure)
        if retard is not None and retard >= RETARD_IMPORTANT_MINUTES:
            resultats.append((vol, retard, nouvelle_heure))

    return resultats


def niveau_affluence(nombre_30_min):
    if nombre_30_min >= 8:
        return "🔴 Forte"
    if nombre_30_min >= 4:
        return "🟠 Moyenne"
    return "🟢 Calme"


def creer_resume(vols):
    dans_30 = vols_dans_delai(vols, 30)
    dans_60 = vols_dans_delai(vols, 60)
    retards = retards_importants(vols)

    t1_30 = sum(1 for v in dans_30 if v["terminal"] == "1")
    t2_30 = sum(1 for v in dans_30 if v["terminal"] == "2")

    t1_60 = sum(1 for v in dans_60 if v["terminal"] == "1")
    t2_60 = sum(1 for v in dans_60 if v["terminal"] == "2")

    message = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n\n"
        f"🕒 Mise à jour : {maintenant_paris().strftime('%H:%M')}\n"
        f"🚖 Activité : <b>{niveau_affluence(len(dans_30))}</b>\n"
        f"⚠️ Retards importants : <b>{len(retards)}</b>\n\n"
        f"⏱️ <b>Dans les 30 min :</b> {len(dans_30)} vols\n"
        f"🔵 Terminal 1 : {t1_30}\n"
        f"🟣 Terminal 2 : {t2_30}\n\n"
        f"🕐 <b>Dans la prochaine heure :</b> {len(dans_60)} vols\n"
        f"🔵 Terminal 1 : {t1_60}\n"
        f"🟣 Terminal 2 : {t2_60}\n\n"
    )

    if dans_30:
        message += "🛬 <b>Prochains vols :</b>\n"
        for vol in dans_30[:10]:
            message += (
                f"• {vol['heure_prevue']} - {vol['ville']} - "
                f"{vol['compagnie']} - {badge_terminal(vol['terminal'])} - {vol['statut_fr']}\n"
            )
    else:
        message += "Aucun vol prévu dans les 30 prochaines minutes."

    return message


# =========================
# ALERTES
# =========================

def cle_vol(vol):
    return f"{vol['numero_vol']}-{vol['heure_prevue']}-{vol['terminal']}"


def cle_retard(vol):
    return f"{vol['numero_vol']}-{vol['heure_prevue']}-{vol.get('heure_reelle')}-{vol['terminal']}"


def initialiser_sans_spam(vols):
    for vol in vols:
        if statut_est_arrive(vol["statut"]):
            vols_deja_annonces.add(cle_vol(vol))

        if statut_est_retarde(vol["statut"]):
            retards_deja_annonces.add(cle_retard(vol))


def envoyer_alertes_nouvelles_arrivees(vols):
    dans_30 = vols_dans_delai(vols, 30)
    nb_30 = len(dans_30)

    for vol in vols:
        if not statut_est_arrive(vol["statut"]):
            continue

        cle = cle_vol(vol)
        if cle in vols_deja_annonces:
            continue

        message = (
            "🚨 <b>NOUVELLE ARRIVÉE</b> 🚨\n\n"
            f"✈️ <b>{vol['compagnie']}</b>\n"
            f"🌍 Provenance : <b>{vol['ville']}</b>\n"
            f"📍 {badge_terminal(vol['terminal'])}\n"
            f"🕒 Heure prévue : {vol['heure_prevue']}\n"
            f"📌 Statut : {vol['statut_fr']}\n\n"
            f"🚖 Vols dans les 30 prochaines minutes : <b>{nb_30}</b>"
        )

        envoyer_telegram(message)
        vols_deja_annonces.add(cle)


def envoyer_alertes_retards(vols):
    for vol, retard, nouvelle_heure in retards_importants(vols):
        cle = cle_retard(vol)
        if cle in retards_deja_annonces:
            continue

        message = (
            "⚠️ <b>RETARD IMPORTANT</b> ⚠️\n\n"
            f"✈️ <b>{vol['compagnie']}</b>\n"
            f"🌍 Provenance : <b>{vol['ville']}</b>\n"
            f"📍 {badge_terminal(vol['terminal'])}\n"
            f"🕒 Heure prévue : {vol['heure_prevue']}\n"
            f"⏰ Nouvelle heure : {nouvelle_heure}\n"
            f"⌛ Retard : <b>{retard} min</b>\n\n"
            "🚖 Les chauffeurs peuvent adapter leur position."
        )

        envoyer_telegram(message)
        retards_deja_annonces.add(cle)


# =========================
# BOUCLE PRINCIPALE
# =========================

def boucle_principale():
    global dernier_resume

    ignorer_anciennes_commandes()

    envoyer_telegram(
        "✅ <b>EasyTaxi Flight Alert V4 lancé</b>\n"
        "Arrivées + retards importants activés.\n\n"
        "Commande disponible : /test"
    )

    try:
        vols = recuperer_vols()
        initialiser_sans_spam(vols)
        envoyer_telegram(creer_resume(vols))
        dernier_resume = maintenant_paris()
    except Exception as e:
        envoyer_telegram(f"⚠️ Erreur au démarrage : {e}")

    while True:
        try:
            vols = recuperer_vols()

            gerer_commandes(vols)
            envoyer_alertes_nouvelles_arrivees(vols)
            envoyer_alertes_retards(vols)

            now = maintenant_paris()
            if dernier_resume is None or (now - dernier_resume).total_seconds() >= FREQUENCE_RESUME_SECONDES:
                envoyer_telegram(creer_resume(vols))
                dernier_resume = now

        except Exception as e:
            print("Erreur boucle principale:", e)
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}")

        time.sleep(FREQUENCE_VERIFICATION_SECONDES)


if __name__ == "__main__":
    boucle_principale()
