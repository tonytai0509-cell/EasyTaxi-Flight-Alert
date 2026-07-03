

Conversations
Google Workspace
Obtenez une adresse e-mail professionnelle du type "@votre-entreprise.com"
Plus 30 Go d'espace de stockage par utilisateur, des appels vidéo plus longs et d'autres avantages avec Google Workspace
45 % sur 100 Go utilisés
Conditions d'utilisation · Confidentialité · Règlement du programme
Dernière activité sur le compte : il y a 1 minute
Détails
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs
")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004321946575")

URL_ARRIVEES = "https://www.nice.aeroport.fr/en/flights/arrivals"

FREQUENCE_VERIFICATION_SECONDES = 300      # 5 minutes
FREQUENCE_RESUME_SECONDES = 1800           # 30 minutes

PARIS = ZoneInfo("Europe/Paris")

vols_deja_annonces = set()
dernier_resume = None


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


# =========================
# OUTILS
# =========================

def maintenant_paris():
    return datetime.now(PARIS)


def nettoyer(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def statut_est_arrive(statut):
    s = statut.lower()
    mots = ["arrived", "landing", "landed", "atterri", "arrivé", "arrive"]
    return any(mot in s for mot in mots)


def statut_est_annule(statut):
    s = statut.lower()
    return "cancel" in s or "annul" in s


def convertir_heure_du_jour(heure_txt):
    h, m = map(int, heure_txt.split(":"))
    now = maintenant_paris()
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


# =========================
# RECUPERATION DES VOLS
# =========================

def recuperer_lignes_page():
    headers = {
        "User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/2.0"
    }
    html = requests.get(URL_ARRIVEES, headers=headers, timeout=25).text
    soup = BeautifulSoup(html, "html.parser")

    texte = soup.get_text("\n")
    lignes = [nettoyer(x) for x in texte.split("\n")]
    lignes = [x for x in lignes if x]
    return lignes


def extraire_vols_depuis_lignes(lignes):
    vols = []

    for i, ligne in enumerate(lignes):
        # Un bloc de vol commence souvent par l'heure prévue
        if not re.fullmatch(r"\d{1,2}:\d{2}", ligne):
            continue

        bloc = lignes[i:i+25]
        if len(bloc) < 5:
            continue

        heure_prevue = bloc[0]

        # Ville de provenance : souvent ligne juste après l'heure
        ville = bloc[1] if len(bloc) > 1 else "Inconnue"

        # Terminal
        terminal = None
        for x in bloc:
            if x in ["1", "2"]:
                terminal = x
                break

        # Numéro de vol
        numeros = []
        for x in bloc:
            if re.fullmatch(r"[A-Z0-9]{2,3}\s?\d{2,5}[A-Z]?", x):
                numeros.append(x.replace(" ", ""))

        numero_vol = numeros[0] if numeros else "N/A"

        # Statut
        statut = ""
        mots_statut = [
            "Arrived", "Expected", "Delayed", "Landing", "Cancelled",
            "On time", "Last call", "Boarding"
        ]
        for x in bloc:
            if any(mot.lower() in x.lower() for mot in mots_statut):
                statut = x
                break

        # Compagnie : heuristique simple
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

        # On garde uniquement les vols exploitables T1/T2
        if terminal in ["1", "2"] and statut:
            vols.append({
                "heure_prevue": heure_prevue,
                "ville": ville,
                "numero_vol": numero_vol,
                "compagnie": compagnie,
                "terminal": terminal,
                "statut": statut,
            })

    # Déduplication
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


def niveau_affluence(nombre_30_min):
    if nombre_30_min >= 8:
        return "🔴 Forte"
    if nombre_30_min >= 4:
        return "🟠 Moyenne"
    return "🟢 Calme"


def creer_resume(vols):
    dans_30 = vols_dans_delai(vols, 30)
    dans_60 = vols_dans_delai(vols, 60)

    t1_30 = sum(1 for v in dans_30 if v["terminal"] == "1")
    t2_30 = sum(1 for v in dans_30 if v["terminal"] == "2")

    t1_60 = sum(1 for v in dans_60 if v["terminal"] == "1")
    t2_60 = sum(1 for v in dans_60 if v["terminal"] == "2")

    message = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n\n"
        f"🕒 Mise à jour : {maintenant_paris().strftime('%H:%M')}\n"
        f"🚖 Activité : <b>{niveau_affluence(len(dans_30))}</b>\n\n"
        f"⏱️ <b>Dans les 30 min :</b> {len(dans_30)} vols\n"
        f"• Terminal 1 : {t1_30}\n"
        f"• Terminal 2 : {t2_30}\n\n"
        f"🕐 <b>Dans la prochaine heure :</b> {len(dans_60)} vols\n"
        f"• Terminal 1 : {t1_60}\n"
        f"• Terminal 2 : {t2_60}\n\n"
    )

    if dans_30:
        message += "🛬 <b>Prochains vols :</b>\n"
        for vol in dans_30[:10]:
            message += (
                f"• {vol['heure_prevue']} - {vol['ville']} - "
                f"{vol['numero_vol']} - T{vol['terminal']} - {vol['statut']}\n"
            )
    else:
        message += "Aucun vol prévu dans les 30 prochaines minutes."

    return message


# =========================
# ALERTES
# =========================

def cle_vol(vol):
    return f"{vol['numero_vol']}-{vol['heure_prevue']}-{vol['terminal']}"


def initialiser_sans_spam(vols):
    """
    Au démarrage, on marque les vols déjà arrivés comme connus.
    Comme ça, le bot ne spamme pas 20 anciens atterrissages.
    """
    for vol in vols:
        if statut_est_arrive(vol["statut"]):
            vols_deja_annonces.add(cle_vol(vol))


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
            "🛬 <b>NOUVELLE ARRIVÉE</b>\n\n"
            f"✈️ <b>{vol['numero_vol']}</b>\n"
            f"🏢 {vol['compagnie']}\n"
            f"🌍 Provenance : {vol['ville']}\n"
            f"📍 Terminal : <b>{vol['terminal']}</b>\n"
            f"🕒 Heure prévue : {vol['heure_prevue']}\n"
            f"📌 Statut : {vol['statut']}\n\n"
            f"🚖 Vols dans les 30 prochaines minutes : <b>{nb_30}</b>"
        )

        envoyer_telegram(message)
        vols_deja_annonces.add(cle)


# =========================
# BOUCLE PRINCIPALE
# =========================

def boucle_principale():
    global dernier_resume

    envoyer_telegram("✅ <b>EasyTaxi Flight Alert V2 lancé</b>\nSurveillance des arrivées T1 + T2 activée.")

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
            envoyer_alertes_nouvelles_arrivees(vols)

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
