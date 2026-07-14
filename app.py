import os
import re
import time
import json
import html
import logging
import logging.handlers
import sqlite3
import threading
import unicodedata
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =========================
# LOGGING
# =========================

LOG_FICHIER = os.getenv("LOG_FICHIER", "easytaxi.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(LOG_FICHIER, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("easytaxi")

# =========================
# CONFIGURATION
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PARIS = ZoneInfo("Europe/Paris")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN et TELEGRAM_CHAT_ID doivent être définis en variables d'environnement.")

URL_ESI_VOLS = "https://www.nice.aeroport.fr/en/esi/flights/rows"
URL_SITE_CLASSIQUE = "https://www.nice.aeroport.fr/en/flights/arrivals"

# Site officiel de l'aéroport = gratuit, seule source utilisée pour les vols
FREQUENCE_SITE_SECONDES = 60

FREQUENCE_RESUME_SECONDES = 1800

# ---- Horaires fixes du gros résumé (13h00, 13h30, 14h00...) et pause nocturne ----
# Pas de gros résumé entre 01:30 et 07:29 inclus ; reprise pile à 07:30.
PAUSE_RESUME_HEURE_DEBUT = (1, 30)
PAUSE_RESUME_HEURE_FIN = (7, 30)
dernier_slot_resume = None

# ---- Résumé spécial 23h (pour les plus courageux) : tout ce qui reste cette nuit,
# retards inclus, jusqu'à 4h du matin (même après minuit) ----
HEURE_RESUME_NUIT = 23
MINUTE_RESUME_NUIT = 30  # décalé de 23h à 23h30 pour ne pas se cumuler au résumé horaire de 23h00
dernier_resume_nuit_date = None

# ---- Espacement des petites alertes (approche/posé/retard) la nuit : entre 2h et 7h,
# on regroupe et n'envoie qu'toutes les 15 min au lieu d'en continu ----
NUIT_ESPACEMENT_HEURE_DEBUT = 2
NUIT_ESPACEMENT_HEURE_FIN = 7
NUIT_ESPACEMENT_SECONDES = 15 * 60
dernier_envoi_alertes_nuit = None
RETARD_IMPORTANT_MINUTES = 20

# ---- SNCF / TGV (API officielle gratuite, quota très généreux : 150k/mois) ----
SNCF_API_TOKEN = os.getenv("SNCF_API_TOKEN")
SNCF_GARE_NOM = os.getenv("SNCF_GARE_NOM", "Nice Ville")
SNCF_STOP_AREA_ID = os.getenv("SNCF_STOP_AREA_ID")  # optionnel : évite une résolution auto si déjà connu
SNCF_INCLURE_OUIGO = os.getenv("SNCF_INCLURE_OUIGO", "true").lower() == "true"
SNCF_INCLURE_TER = os.getenv("SNCF_INCLURE_TER", "true").lower() == "true"
FREQUENCE_SNCF_SECONDES = int(os.getenv("FREQUENCE_SNCF_SECONDES", "120"))  # 2 min, large quota donc pas besoin d'économiser
RETARD_TRAIN_IMPORTANT_MINUTES = int(os.getenv("RETARD_TRAIN_IMPORTANT_MINUTES", "15"))
APPROCHE_TRAIN_MINUTES = int(os.getenv("APPROCHE_TRAIN_MINUTES", "10"))

trains_cache = []
derniere_maj_trains = None
_stop_area_id_resolu = None
origine_cache = {}  # vehicle_journey_id -> {"origine": str, "time": datetime}

trains_approche_annonces = set()
trains_arrives_annonces = set()
trains_annules_annonces = set()
trains_retard_annonces = {}

# ---- Circulation Alpes-Maritimes (Inforoutes06, flux RSS officiels et gratuits du
# Département — zéro clé API, zéro quota) : corridor Monaco → Nice → Cannes par l'A8 ----
URLS_RSS_CIRCULATION = {
    "Menton/Roya-Bévéra": "https://www.inforoutes06.fr/rss-menton-roya-bevera.php",
    "Est littoral (Èze)": "https://www.inforoutes06.fr/rss-est-littoral.php",
    "Nice": "https://www.inforoutes06.fr/rss-nice.php",
    "Littoral ouest Antibes": "https://www.inforoutes06.fr/rss-littoral-ouest-antibes.php",
    "Littoral ouest Cannes": "https://www.inforoutes06.fr/rss-littoral-ouest-cannes.php",
}
FREQUENCE_CIRCULATION_SECONDES = int(os.getenv("FREQUENCE_CIRCULATION_SECONDES", "600"))  # 10 min

# On remonte : accident, bouchon/ralentissement, fermeture, et maintenant aussi les travaux
# en général (plus seulement ceux qui ferment la route).
MOTS_CIRCULATION_IMPORTANTS = [
    "accident",
    "bouchon", "ralentissement", "embouteillage", "circulation dense", "circulation difficile",
    "fermé", "fermée", "fermeture", "coupé", "coupée", "coupure",
    "travaux",
]

CIRCULATION_FICHIER = os.getenv("CIRCULATION_FICHIER", "circulation_vues.json")

circulation_cache = []
derniere_maj_circulation = None
circulation_guids_vus = set()


DB_FICHIER = os.getenv("DB_FICHIER", "historique_vols.db")

# ---- Persistance des mémoires anti-doublon (évite les alertes répétées après un redémarrage) ----
ETATS_FICHIER = os.getenv("ETATS_FICHIER", "etats_alertes.json")
ABONNEMENTS_VOL_FICHIER = os.getenv("ABONNEMENTS_VOL_FICHIER", "abonnements_vol.json")

# ---- Compteur de voitures par terminal (signalé par les chauffeurs) ----
FILE_FICHIER = os.getenv("FILE_FICHIER", "file_attente.json")

# ---- Commandes Telegram (polling gratuit, données cache uniquement) ----
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
dernier_update_id = None

# ---- Résumé du matin ----
RESUME_MATIN_HEURE = int(os.getenv("RESUME_MATIN_HEURE", "6"))
dernier_resume_matin = None

# ---- Top 5 des annonces de voitures, envoyé chaque soir ----
STATS_FICHIER = os.getenv("STATS_FICHIER", "stats_annonces.json")
HEURE_STATS_SOIR = int(os.getenv("HEURE_STATS_SOIR", "22"))
dernier_stats_soir = None

# ---- Watchdog scraping site ----
WATCHDOG_SEUIL_ECHECS = 3
echecs_site_consecutifs = 0
alerte_watchdog_envoyee = False
# Heures creuses où 0 vol est normal (pas un signe de site cassé) : pas d'alerte watchdog
WATCHDOG_HEURE_DEBUT_SILENCE = int(os.getenv("WATCHDOG_HEURE_DEBUT_SILENCE", "1"))
WATCHDOG_HEURE_FIN_SILENCE = int(os.getenv("WATCHDOG_HEURE_FIN_SILENCE", "5"))

# ---- Watchdog API SNCF ----
WATCHDOG_SNCF_SEUIL_ECHECS = int(os.getenv("WATCHDOG_SNCF_SEUIL_ECHECS", "3"))
echecs_sncf_consecutifs = 0
alerte_watchdog_sncf_envoyee = False

# ---- Nettoyage quotidien des caches ----
dernier_nettoyage = None

# ---- Annulations ----
annules_deja_annonces = set()

vols_cache = []
derniere_maj_site = None
dernier_resume = None

# ---- Anti-régression de statut : un vol qui a déjà été vu "en approche" ou "posé"
# ne doit jamais redescendre à "en route"/"prévu" au scan suivant, même si le site
# aéroport se contredit (donnée bruitée côté source, pas la réalité du vol) ----
meilleur_statut_vu = {}  # cle_vol -> (niveau, site_status)

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


def nettoyer(texte):
    return re.sub(r"\s+", " ", texte or "").strip()


def emoji_terminal(terminal):
    terminal = str(terminal or "").strip()
    if terminal == "1":
        return "🔵T1"
    if terminal == "2":
        return "🔴T2"
    return "⚪T?"


def label_terminal(terminal):
    if str(terminal) == "1":
        return "Terminal 1"
    if str(terminal) == "2":
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
    return "arriv" in s or "landed" in s or "atterri" in s or "posé" in s


def est_approche(status):
    s = (status or "").lower()
    return "approach" in s or "landing" in s or "final" in s or "approche" in s


def est_en_route(status):
    s = (status or "").lower()
    return "enroute" in s or "en route" in s or "departed" in s or "décollé" in s


def est_arrive_ou_approche(status):
    return est_pose(status) or est_approche(status)


def niveau_progression(status):
    """Ordonne les statuts pour empêcher un vol de 'reculer' : posé > approche > en route > prévu.
    Un statut annulé n'entre pas dans cette progression (traité séparément)."""
    if est_pose(status):
        return 3
    if est_approche(status):
        return 2
    if est_en_route(status):
        return 1
    return 0


def appliquer_ratchet_statuts(vols):
    """Empêche un vol de redescendre de statut (ex: 'en approche' -> 'en route') entre deux scans,
    ce qui arrive quand le site aéroport se contredit d'un scan à l'autre (donnée bruitée),
    pas parce que le vol a vraiment reculé. Une fois annulé, le statut reste annulé."""
    for v in vols:
        status = v.get("site_status")
        if est_annule(status):
            continue  # annulation traitée à part, pas de ratchet dessus
        cle = cle_vol(v)
        niveau_actuel = niveau_progression(status)
        precedent = meilleur_statut_vu.get(cle)
        if precedent is None or niveau_actuel >= precedent[0]:
            meilleur_statut_vu[cle] = (niveau_actuel, status)
        else:
            v["site_status"] = precedent[1]
    return vols


SEUIL_POSE_FORCE_MINUTES = 20  # au-delà, on considère le vol posé même si le site dit encore "Landing"/"Expected"


def corriger_statuts_bloques(vols):
    """Le site aéroport reste parfois bloqué sur 'Landing'/'Expected' bien après l'atterrissage
    réel (donnée qui ne se met pas à jour côté source). Si l'heure prévue/estimée est dépassée
    de plus de SEUIL_POSE_FORCE_MINUTES et que le vol n'est ni annulé ni déjà marqué posé,
    on force le statut à 'posé' pour ne pas rater indéfiniment l'alerte correspondante."""
    for v in vols:
        status = v.get("site_status")
        dt_actuel = v.get("dt_actuel")
        if not dt_actuel or est_annule(status) or est_pose(status):
            continue
        minutes_ecoulees = (maintenant() - dt_actuel.astimezone(PARIS)).total_seconds() / 60
        if minutes_ecoulees >= SEUIL_POSE_FORCE_MINUTES:
            v["site_status"] = f"Arrived {v.get('actuel', '')}".strip()
    return vols


def statut_lisible(v):
    status = v.get("site_status") or v.get("live_status") or v.get("status") or ""
    retard = v.get("retard", 0)

    if est_pose(status):
        return f"✅ Posé {v['actuel']}"
    if est_approche(status):
        return "🛬 Approche"
    if est_en_route(status):
        return "✈️ En route"
    if "cancel" in status.lower() or "annul" in status.lower():
        return "❌ Annulé"
    if "delay" in status.lower() or "retard" in status.lower() or retard >= RETARD_IMPORTANT_MINUTES:
        return f"⏰ +{retard}min" if retard > 0 else "⏰ Retard"
    if retard >= 10:
        return f"⏰ +{retard}min"
    return "🟢 Prévu"


def heure_lisible(v):
    if v["prevu"] != "N/A" and v["actuel"] != "N/A" and v["prevu"] != v["actuel"]:
        return f"{v['prevu']}→{v['actuel']}"
    return v["actuel"] if v["actuel"] != "N/A" else v["prevu"]


def cle_vol(v):
    return f"{v.get('numero')}-{v.get('provenance')}-{v.get('terminal')}-{v.get('prevu')}"


def envoyer_telegram(message, silencieux=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silencieux,
                "reply_markup": json.dumps(clavier_permanent()),  # re-rattache le bouton fixe à chaque envoi
            },
            timeout=20,
        )
        if not r.ok:
            logger.error(f"Erreur Telegram: {r.text}")
    except Exception as e:
        logger.error(f"Erreur Telegram: {e}")


def est_heure(x):
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", x or ""))


def normaliser_terminal(x):
    x = (x or "").upper().strip()
    if x in ["1", "T1", "TERMINAL 1"]:
        return "1"
    if x in ["2", "T2", "TERMINAL 2"]:
        return "2"
    return None


def statut_site_lisible(x):
    s = (x or "").lower()
    heure = extraire_heure(x)
    if "arrived" in s or "atterri" in s:
        return f"Arrived {heure}" if heure else "Arrived"
    if "landing" in s or "approche" in s:
        return f"Landing {heure}" if heure else "Landing"
    if "delayed" in s or "retard" in s:
        return f"Delayed {heure}" if heure else "Delayed"
    if "expected" in s or "prévu" in s or "prevu" in s:
        return f"Expected {heure}" if heure else "Expected"
    return x


def extraire_heure(x):
    heures = re.findall(r"\b\d{1,2}:\d{2}\b", x or "")
    return heures[-1] if heures else None


def premiere_ligne_cellule(cellule):
    """Extrait la première ligne de texte utile d'une cellule de tableau.
    Certaines cellules (provenance, compagnie, n° de vol) empilent plusieurs
    lignes quand un vol est partagé entre plusieurs compagnies (codeshare) —
    on ne garde que la première, qui correspond au vol principal."""
    if cellule is None:
        return ""
    lignes = [nettoyer(x) for x in cellule.get_text("\n").split("\n") if nettoyer(x)]
    return lignes[0] if lignes else ""


def _parser_tableau_vols_html(html_texte, date_demandee):
    """Parsing commun aux deux sources de vols (endpoint complet ESI et page classique) —
    même structure de tableau HTML des deux côtés (js-flight-time, js-terminal, etc.)."""
    soup = BeautifulSoup(html_texte, "html.parser")

    vols = []
    for cellule_heure in soup.find_all("td", class_="js-flight-time"):
        tr = cellule_heure.find_parent("tr")
        if not tr:
            continue

        try:
            h = int(cellule_heure.get("data-hour"))
            m = int(cellule_heure.get("data-min"))
        except (TypeError, ValueError):
            continue
        heure = f"{h:02d}:{m:02d}"

        cellule_terminal = tr.find("td", class_="js-terminal")
        terminal = normaliser_terminal(cellule_terminal.get_text(strip=True)) if cellule_terminal else None
        if terminal not in ("1", "2"):
            continue

        cellules = tr.find_all("td")

        div_ville = cellules[1].find("div", class_="item") if len(cellules) > 1 else None
        ville = nettoyer_nom(div_ville.get_text(strip=True)) if div_ville else "N/A"

        p_compagnie = cellules[2].find("p") if len(cellules) > 2 else None
        compagnie = nettoyer_compagnie(p_compagnie.get_text(strip=True)) if p_compagnie else "N/A"

        p_numero = cellules[3].find("p") if len(cellules) > 3 else None
        numero = p_numero.get_text(strip=True).replace(" ", "") if p_numero else "N/A"

        p_status = cellules[5].find("p") if len(cellules) > 5 else None
        status_brut = nettoyer(p_status.get_text(strip=True)) if p_status else ""
        status = statut_site_lisible(status_brut) if status_brut else "Expected"

        heure_status = extraire_heure(status)
        actuel = heure_status or heure
        try:
            h_prevu, m_prevu = map(int, heure.split(":"))
            dt_prevu = datetime(date_demandee.year, date_demandee.month, date_demandee.day, h_prevu, m_prevu, tzinfo=PARIS)
        except (ValueError, AttributeError):
            dt_prevu = None
        retard = minutes_retard(heure, actuel)
        dt_actuel = (dt_prevu + timedelta(minutes=retard)) if dt_prevu else None

        vols.append({
            "numero": numero or "N/A",
            "compagnie": compagnie or "N/A",
            "provenance": ville,
            "terminal": terminal,
            "status": "Expected",
            "live_status": None,
            "site_status": status,
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": heure,
            "actuel": actuel,
            "retard": retard,
            "source": "site"
        })

    return vols


def recuperer_vols_site():
    """Récupère TOUS les vols de la journée (00h05 à 23h35) via l'endpoint ESI
    utilisé par le site lui-même pour charger ses tranches horaires — la page
    /en/flights/arrivals classique ne renvoie que les 15 premiers résultats.

    La date est fixée par le paramètre envoyé à l'API — aucune ambiguïté à deviner ici,
    contrairement à l'ancienne page qui ne montrait qu'un extrait autour de l'heure courante.

    En soirée (20h-23h59), on va aussi chercher le tout début du lendemain (00h-06h) :
    ces vols appartiennent à une date différente côté API et resteraient sinon invisibles
    jusqu'à ce que l'horloge passe minuit — ce qui viderait à tort le résumé de nuit de 23h."""
    aujourdhui = maintenant().date()
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}

    url = f"{URL_ESI_VOLS}?direction=A&terminal=All&date={aujourdhui.strftime('%Y%m%d')}"
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    vols = _parser_tableau_vols_html(r.text, aujourdhui)

    if maintenant().hour >= 20:
        # Le site liste parfois déjà les tout premiers vols du lendemain (00h-06h) dans le
        # tableau "aujourd'hui" lui-même (vu sur l'écran d'affichage de l'aéroport). Comme on
        # leur a donné la date d'aujourd'hui, ils paraissent "déjà passés" une fois qu'il est
        # tard le soir et disparaissent à tort des résumés/commandes. On corrige : le soir,
        # une heure < 6h dans le lot "aujourd'hui" appartient en réalité à demain.
        for v in vols:
            statut = v.get("site_status") or v.get("live_status") or v.get("status")
            if est_arrive_ou_approche(statut):
                continue  # jamais toucher un vol déjà posé/en approche : sécurité anti-régression
            for champ in ("dt_prevu", "dt_actuel"):
                dt = v.get(champ)
                if dt and dt.hour < 6:
                    v[champ] = dt + timedelta(days=1)

        demain = aujourdhui + timedelta(days=1)
        try:
            url_demain = f"{URL_ESI_VOLS}?direction=A&terminal=All&date={demain.strftime('%Y%m%d')}"
            r2 = requests.get(url_demain, headers=headers, timeout=25)
            r2.raise_for_status()
            vols_demain = _parser_tableau_vols_html(r2.text, demain)
            vols += [v for v in vols_demain if v.get("dt_prevu") and v["dt_prevu"].hour < 6]
        except Exception as e:
            logger.warning(f"Erreur scraping vols lendemain (nuit) : {e}")

    if maintenant().hour < 6:
        # Symétrique du cas du soir : tôt le matin, le site peut ne pas avoir encore basculé
        # son propre jour "opérationnel" sur la date du jour — un vol du tout petit matin
        # (ex: prévu 00h10, retardé à 01h40) peut alors rester filé sous la date d'HIER côté
        # site, et donc absent du lot "aujourd'hui" qu'on vient de récupérer. On va chercher
        # hier au cas où, et on corrige la date des vols de tout petit matin qu'on y trouve.
        hier = aujourdhui - timedelta(days=1)
        try:
            url_hier = f"{URL_ESI_VOLS}?direction=A&terminal=All&date={hier.strftime('%Y%m%d')}"
            r3 = requests.get(url_hier, headers=headers, timeout=25)
            r3.raise_for_status()
            vols_hier = _parser_tableau_vols_html(r3.text, hier)
            for v in vols_hier:
                if v.get("dt_prevu") and v["dt_prevu"].hour < 6:
                    for champ in ("dt_prevu", "dt_actuel"):
                        if v.get(champ):
                            v[champ] = v[champ] + timedelta(days=1)
                    vols.append(v)
        except Exception as e:
            logger.warning(f"Erreur scraping vols veille (petit matin) : {e}")

    return dedoublonner_vols(vols)


def recuperer_vols_site_classique():
    """Page classique (/en/flights/arrivals) : ne montre qu'une quinzaine de vols proches
    de l'heure actuelle, mais son statut ('Landing'/'Arrived') semble se mettre à jour plus
    vite que le fragment ESI complet, qui reste parfois bloqué sur un vieux statut. Utilisée
    uniquement pour rafraîchir le statut des vols déjà connus, pas pour la couverture complète."""
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    r = requests.get(URL_SITE_CLASSIQUE, headers=headers, timeout=20)
    r.raise_for_status()
    return _parser_tableau_vols_html(r.text, maintenant().date())


def fusionner_statuts_recents(vols_principal, vols_classique):
    """Pour les vols présents dans les deux listes, on fait confiance à la page classique
    (plus proche du temps réel) pour le statut ET pour l'heure estimée, quand elle est plus
    précise que l'endpoint ESI (qui reste parfois bloqué sur 'Delayed' sans heure alors que
    la page classique a déjà 'Delayed HH:MM'). On ne reprend jamais sa date : seulement
    l'heure HH:MM, recombinée avec la date déjà résolue côté principal — pour ne pas
    réintroduire le bug de date autour de minuit qu'on a corrigé par ailleurs."""
    classiques = {cle_vol(v): v for v in vols_classique}
    for v in vols_principal:
        c = classiques.get(cle_vol(v))
        if not c:
            continue
        v["site_status"] = c.get("site_status")
        actuel_classique = c.get("actuel")
        # On ne reprend l'heure classique que si elle apporte une info que le principal
        # n'a pas déjà (principal encore sur l'heure prévue = pas de retard détecté).
        if actuel_classique and actuel_classique != c.get("prevu") and v.get("actuel") == v.get("prevu") and v.get("dt_prevu"):
            v["actuel"] = actuel_classique
            v["retard"] = minutes_retard(v["prevu"], actuel_classique)
            v["dt_actuel"] = v["dt_prevu"] + timedelta(minutes=v["retard"])
    return vols_principal


def heure_aujourdhui(hh):
    """Convertit une heure HH:MM en datetime, en gérant le passage de minuit :
    si l'heure obtenue est à plus de 6h dans le passé (ex: il est 23:59 et le vol
    est à 00:25), on suppose que c'est en réalité demain. Symétriquement, si elle
    est à plus de 6h dans le futur (ex: il est 00:30 et le vol affiché est à 23:50),
    on suppose que c'est en réalité hier."""
    try:
        h, m = map(int, hh.split(":"))
        maintenant_dt = maintenant()
        dt = maintenant_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        diff_heures = (dt - maintenant_dt).total_seconds() / 3600
        if diff_heures < -6:
            dt += timedelta(days=1)
        elif diff_heures > 6:
            dt -= timedelta(days=1)
        return dt
    except Exception:
        return None


def minutes_retard(prevu, actuel):
    """Calcule le retard en minutes entre deux heures HH:MM, en gérant le passage à minuit :
    on teste l'heure 'actuel' la veille, le jour même et le lendemain, et on garde
    l'interprétation qui donne l'écart le plus petit (la plus plausible)."""
    try:
        h1, m1 = map(int, prevu.split(":"))
        h2, m2 = map(int, actuel.split(":"))
    except Exception:
        return 0

    base = maintenant().replace(second=0, microsecond=0)
    a = base.replace(hour=h1, minute=m1)

    meilleur_ecart = None
    for jours in (-1, 0, 1):
        b = base.replace(hour=h2, minute=m2) + timedelta(days=jours)
        ecart = (b - a).total_seconds() / 60
        if meilleur_ecart is None or abs(ecart) < abs(meilleur_ecart):
            meilleur_ecart = ecart

    return max(0, int(meilleur_ecart))


# =========================
# AERODATABOX
# =========================

LETTRES_SPECIALES = {
    "Ł": "L", "ł": "l",
    "Đ": "D", "đ": "d",
    "Ø": "O", "ø": "o",
    "Æ": "AE", "æ": "ae",
    "Þ": "TH", "þ": "th",
    "ß": "ss",
}


def normaliser_pour_comparaison(texte):
    """Retire les accents pour comparer deux noms de ville, même s'ils viennent
    de sources différentes qui les orthographient différemment
    (ex: WROCŁAW vs WROCLAW, GDAŃSK vs GDANSK)."""
    texte = texte or ""
    for lettre, remplacement in LETTRES_SPECIALES.items():
        texte = texte.replace(lettre, remplacement)
    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return texte.upper().strip()


def dedoublonner_vols(vols):
    uniques = {}
    for v in vols:
        ville_normalisee = normaliser_pour_comparaison(v.get("provenance"))
        # On inclut dt_actuel (date+heure) et pas juste l'heure ("actuel"), sinon un vol
        # d'aujourd'hui déjà arrivé et un vol de demain à la même heure/ville/terminal
        # se retrouvent avec la même clé et l'un écrase l'autre (bug vols après minuit).
        cle = f"{v.get('dt_actuel')}-{ville_normalisee}-{v.get('terminal')}"
        if cle not in uniques:
            uniques[cle] = v
        else:
            # priorité au site s'il fournit un status posé/approche, sinon compagnie la plus courte
            ancien = uniques[cle]
            if est_arrive_ou_approche(v.get("site_status")) and not est_arrive_ou_approche(ancien.get("site_status")):
                uniques[cle] = v
            elif len(v.get("compagnie", "")) < len(ancien.get("compagnie", "")):
                v["site_status"] = ancien.get("site_status") or v.get("site_status")
                uniques[cle] = v
    resultat = list(uniques.values())
    resultat.sort(key=lambda v: (v.get("dt_actuel") or v.get("dt_prevu") or maintenant(), v.get("terminal", "")))
    return resultat


def _en_heures_creuses():
    heure = maintenant().hour
    return WATCHDOG_HEURE_DEBUT_SILENCE <= heure < WATCHDOG_HEURE_FIN_SILENCE


def _verifier_watchdog_site(vols_site):
    """Alerte si le scraping du site retourne 0 vol plusieurs fois de suite
    (signe probable que la structure du site a changé). Reste silencieux la nuit,
    où 0 vol est parfaitement normal et ne signifie pas que le site est cassé."""
    global echecs_site_consecutifs, alerte_watchdog_envoyee
    if len(vols_site) == 0:
        echecs_site_consecutifs += 1
        if _en_heures_creuses():
            return  # 0 vol la nuit = normal, pas un signe de bug
        logger.warning(f"Scraping site: 0 vol trouvé ({echecs_site_consecutifs}/{WATCHDOG_SEUIL_ECHECS})")
        if echecs_site_consecutifs >= WATCHDOG_SEUIL_ECHECS and not alerte_watchdog_envoyee:
            envoyer_telegram(
                "🚨 <b>Alerte scraping</b>\n"
                "Le site aéroport ne retourne plus aucun vol depuis plusieurs vérifications.\n"
                "La structure de la page a peut-être changé — vérification manuelle recommandée.",
                silencieux=True
            )
            alerte_watchdog_envoyee = True
    else:
        echecs_site_consecutifs = 0
        alerte_watchdog_envoyee = False


def mettre_a_jour_cache_si_besoin(force=False):
    """Récupère les vols depuis le site officiel de l'aéroport (gratuit) — seule source."""
    global vols_cache, derniere_maj_site

    if force or derniere_maj_site is None or (maintenant() - derniere_maj_site).total_seconds() >= FREQUENCE_SITE_SECONDES:
        try:
            vols_bruts = recuperer_vols_site()
            try:
                vols_classique = recuperer_vols_site_classique()
                vols_bruts = fusionner_statuts_recents(vols_bruts, vols_classique)
            except Exception as e:
                # La page classique est un bonus (statut plus frais) : si elle échoue,
                # on continue quand même avec les données de l'endpoint complet seul.
                logger.warning(f"Erreur page classique (statuts non rafraîchis) : {e}")
            vols_cache = corriger_statuts_bloques(appliquer_ratchet_statuts(vols_bruts))
            derniere_maj_site = maintenant()
            _verifier_watchdog_site(vols_cache)
            nb_t1 = sum(1 for v in vols_cache if v.get("terminal") == "1")
            nb_t2 = sum(1 for v in vols_cache if v.get("terminal") == "2")
            logger.info(f"Scraping site: {len(vols_cache)} vols trouvés (T1={nb_t1}, T2={nb_t2})")
            for v in vols_cache:
                logger.info(
                    f"  DETAIL vol: terminal={v.get('terminal')!r} ville={v.get('provenance')!r} "
                    f"heure={v.get('prevu')!r} actuel={v.get('actuel')!r} "
                    f"dt_actuel={v.get('dt_actuel')!r} statut={v.get('site_status')!r}"
                )
        except Exception as e:
            logger.warning(f"Erreur site aéroport: {e}")

    return vols_cache


# =========================
# SNCF / TGV — Gare de Nice-Ville
# API officielle gratuite (Navitia), quota très généreux (150k/mois).
# Doc : https://www.digital.sncf.com/startup/api
# =========================

def resoudre_gare_sncf():
    """Retourne l'identifiant stop_area de la gare. Utilise SNCF_STOP_AREA_ID s'il est fourni,
    sinon retombe sur l'ID officiel de Nice-Ville (testé et validé)."""
    global _stop_area_id_resolu
    if _stop_area_id_resolu:
        return _stop_area_id_resolu
    _stop_area_id_resolu = SNCF_STOP_AREA_ID or "stop_area:SNCF:87756056"  # Nice-Ville, code UIC 87756056
    return _stop_area_id_resolu


def est_train_suivi(commercial_mode):
    """Détermine si ce train doit être suivi par le bot (TGV, OUIGO, TER selon réglages).
    ZOU! est le nom commercial des TER en région PACA — on le traite comme un TER."""
    cm = (commercial_mode or "").upper()
    if "OUIGO" in cm:
        return SNCF_INCLURE_OUIGO
    if "TER" in cm or "ZOU" in cm:
        return SNCF_INCLURE_TER
    if "TGV" in cm or "INOUI" in cm:
        return True
    return False


def type_court(commercial_mode):
    """Renvoie une étiquette courte TER / TGV / OUIGO à afficher dans les messages."""
    cm = (commercial_mode or "").upper()
    if "OUIGO" in cm:
        return "OUIGO"
    if "TER" in cm or "ZOU" in cm:
        return "TER"
    if "TGV" in cm or "INOUI" in cm:
        return "TGV"
    return cm[:5] if cm else "?"


def parse_datetime_sncf(value):
    """Format Navitia : '20260703T211300' (heure locale, sans timezone explicite)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)
    except Exception:
        return None


def recuperer_origine_reelle(vehicle_journey_id):
    """Interroge le détail du voyage pour trouver la vraie ville d'origine
    (le champ 'direction' des arrivals ne donne que le terminus de la ligne).
    Résultat mis en cache car l'origine d'un même trajet ne change pas dans la journée."""
    cached = origine_cache.get(vehicle_journey_id)
    if cached and (maintenant() - cached["time"]).total_seconds() < 6 * 3600:
        return cached["origine"]

    try:
        url = f"https://api.sncf.com/v1/coverage/sncf/vehicle_journeys/{vehicle_journey_id}"
        r = requests.get(url, auth=(SNCF_API_TOKEN, ""), timeout=15)
        r.raise_for_status()
        vj_list = r.json().get("vehicle_journeys", [])
        if not vj_list:
            return "N/A"
        stop_times = vj_list[0].get("stop_times", [])
        if not stop_times:
            return "N/A"
        origine = stop_times[0].get("stop_point", {}).get("name", "N/A")
        origine_cache[vehicle_journey_id] = {"origine": origine, "time": maintenant()}
        return origine
    except Exception as e:
        logger.warning(f"Erreur récupération origine SNCF: {e}")
        return "N/A"


def recuperer_arrivees_sncf():
    stop_id = resoudre_gare_sncf()
    url = f"https://api.sncf.com/v1/coverage/sncf/stop_areas/{stop_id}/arrivals"
    params = {"count": 30, "duration": 7200}  # fenêtre de 2h
    r = requests.get(url, params=params, auth=(SNCF_API_TOKEN, ""), timeout=20)
    r.raise_for_status()
    data = r.json()

    trains = []
    for item in data.get("arrivals", []):
        infos = item.get("display_informations", {}) or {}
        stop_dt = item.get("stop_date_time", {}) or {}

        commercial_mode = infos.get("commercial_mode", "")
        # Log de diagnostic : montre la vraie valeur envoyée par l'API pour CHAQUE train,
        # même ceux qui seront filtrés ci-dessous. Utile si un nouveau type de train
        # (autre réseau, autre libellé) apparaît un jour et doit être ajouté au filtre.
        logger.info(
            f"SNCF RAW: commercial_mode={commercial_mode!r} "
            f"network={infos.get('network')!r} headsign={infos.get('headsign')!r}"
        )
        if not est_train_suivi(commercial_mode):
            continue

        dt_prevu = parse_datetime_sncf(stop_dt.get("base_arrival_date_time"))
        dt_actuel = parse_datetime_sncf(stop_dt.get("arrival_date_time")) or dt_prevu

        retard = 0
        if dt_prevu and dt_actuel:
            retard = max(0, int((dt_actuel - dt_prevu).total_seconds() // 60))

        annule = str(item.get("status", "")).lower() in ("deleted", "delete")

        vehicle_journey_id = None
        for link in item.get("links", []):
            if link.get("type") == "vehicle_journey":
                vehicle_journey_id = link.get("id")
                break

        provenance = "N/A"
        if vehicle_journey_id:
            provenance = recuperer_origine_reelle(vehicle_journey_id)

        trains.append({
            "numero": infos.get("headsign", "N/A"),
            "provenance": nettoyer_nom(provenance),
            "type": commercial_mode,
            "voie": ((item.get("stop_point") or {}).get("name") or ""),
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": hhmm(dt_prevu),
            "actuel": hhmm(dt_actuel),
            "retard": retard,
            "annule": annule,
        })

    trains.sort(key=lambda t: t.get("dt_actuel") or t.get("dt_prevu") or maintenant())
    return trains


URL_TER_ARRIVEES = "https://www.ter.sncf.com/sud-provence-alpes-cote-d-azur/se-deplacer/prochaines-arrivees/nice-ville-87756056"


def recuperer_retards_ter_html():
    """Page officielle TER SNCF grand public (gratuite, sans clé API) : sert uniquement à
    recouper/affiner les retards, l'API SNCF officielle (Navitia) mettant parfois du temps
    à remonter un retard que cette page affiche déjà ('Retard estimé de X min').
    Renvoie un dict {numero_train: retard_en_minutes}."""
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    r = requests.get(URL_TER_ARRIVEES, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    lignes = [nettoyer(l) for l in soup.get_text("\n").split("\n") if nettoyer(l)]

    retards = {}
    for i, ligne in enumerate(lignes):
        m_mode = re.match(r"(?:Mode\s*)?Train\s+.+?\s+(\d+)$", ligne, re.IGNORECASE)
        if not m_mode:
            continue
        numero = m_mode.group(1)
        for l2 in lignes[i + 1:i + 6]:
            if re.match(r"(?:Mode\s*)?Train\s", l2, re.IGNORECASE):
                break
            m_ret = re.match(r"Retard estimé de (\d+)\s*min", l2, re.IGNORECASE)
            if m_ret:
                retards[numero] = int(m_ret.group(1))
                break
    return retards


def fusionner_retards_ter_html(trains_principal):
    """Améliore (jamais ne dégrade) le retard connu de chaque train si la page TER
    officielle en détecte un plus important que l'API SNCF."""
    try:
        retards_html = recuperer_retards_ter_html()
    except Exception as e:
        logger.warning(f"Erreur page TER SNCF: {e}")
        return trains_principal

    for t in trains_principal:
        retard_html = retards_html.get(str(t.get("numero") or ""))
        if retard_html is not None and retard_html > t.get("retard", 0) and t.get("dt_prevu"):
            t["retard"] = retard_html
            t["dt_actuel"] = t["dt_prevu"] + timedelta(minutes=retard_html)
            t["actuel"] = hhmm(t["dt_actuel"])
    return trains_principal


def _verifier_watchdog_sncf(succes):
    """Alerte si l'API SNCF échoue plusieurs fois de suite (token expiré, API en panne, etc.)."""
    global echecs_sncf_consecutifs, alerte_watchdog_sncf_envoyee
    if not succes:
        echecs_sncf_consecutifs += 1
        logger.warning(f"Echec API SNCF ({echecs_sncf_consecutifs}/{WATCHDOG_SNCF_SEUIL_ECHECS})")
        if echecs_sncf_consecutifs >= WATCHDOG_SNCF_SEUIL_ECHECS and not alerte_watchdog_sncf_envoyee:
            envoyer_telegram(
                "🚨 <b>Alerte SNCF</b>\n"
                "L'API SNCF échoue depuis plusieurs vérifications.\n"
                "Le module TGV est peut-être en panne (token expiré ?) — vérification manuelle recommandée.",
                silencieux=True
            )
            alerte_watchdog_sncf_envoyee = True
    else:
        echecs_sncf_consecutifs = 0
        alerte_watchdog_sncf_envoyee = False


def mettre_a_jour_cache_trains_si_besoin(force=False):
    global trains_cache, derniere_maj_trains

    if not SNCF_API_TOKEN:
        logger.info("SNCF: module désactivé (SNCF_API_TOKEN absent)")
        return trains_cache  # module désactivé tant qu'aucun token n'est fourni

    if not force and derniere_maj_trains is not None and (maintenant() - derniere_maj_trains).total_seconds() < FREQUENCE_SNCF_SECONDES:
        return trains_cache

    try:
        trains_cache = recuperer_arrivees_sncf()
        trains_cache = fusionner_retards_ter_html(trains_cache)
        derniere_maj_trains = maintenant()
        _verifier_watchdog_sncf(True)
        logger.info(f"SNCF: {len(trains_cache)} trains trouvés")
        for t in trains_cache:
            logger.info(
                f"  DETAIL train: numero={t.get('numero')!r} type={t.get('type')!r} provenance={t.get('provenance')!r} "
                f"prevu={t.get('prevu')!r} actuel={t.get('actuel')!r} "
                f"dt_actuel={t.get('dt_actuel')!r} annule={t.get('annule')!r}"
            )
    except Exception as e:
        logger.warning(f"Erreur API SNCF: {e}")
        _verifier_watchdog_sncf(False)

    return trains_cache


def charger_circulation_vues():
    try:
        with open(CIRCULATION_FICHIER, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def sauver_circulation_vues(guids):
    try:
        with open(CIRCULATION_FICHIER, "w", encoding="utf-8") as f:
            json.dump(list(guids), f)
    except Exception as e:
        logger.warning(f"Erreur sauvegarde circulation: {e}")


URLS_VINCI_A8 = [
    "https://radio.vinci-autoroutes.com/rubrique/infotrafic",
    "https://radio.vinci-autoroutes.com/rubrique/travaux",
]

# Mots-clés de lieu pour ne garder que les entrées VINCI qui concernent vraiment le
# corridor Monaco → Cannes (le flux VINCI couvre tout le réseau national).
MOTS_CORRIDOR_A8 = [
    "nice", "antibes", "cannes", "monaco", "menton", "la turbie",
    "villeneuve-loubet", "cagnes", "saint-laurent-du-var", "alpes-maritimes",
]


def _circulation_est_importante(titre, description):
    texte = f"{titre} {description}".lower()
    return any(mot in texte for mot in MOTS_CIRCULATION_IMPORTANTS)


def recuperer_alertes_vinci_a8():
    """Complément à Inforoutes06 : le site VINCI Autoroutes (gratuit, sans clé API)
    remonte parfois des travaux/fermetures plus vite ou en plus. On filtre pour ne garder
    que les entrées sur l'A8 concernant le corridor Monaco → Cannes."""
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    alertes = []
    for url in URLS_VINCI_A8:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/article/" not in href:
                    continue
                texte = nettoyer(a.get_text(" "))
                if not texte:
                    continue
                texte_bas = texte.lower()
                if "a8" not in texte_bas:
                    continue
                if not any(mot in texte_bas for mot in MOTS_CORRIDOR_A8):
                    continue
                guid = href if href.startswith("http") else f"https://radio.vinci-autoroutes.com{href}"
                alertes.append({
                    "zone": "VINCI A8",
                    "titre": texte[:120],
                    "description": texte,
                    "guid": guid,
                    "importante": _circulation_est_importante(texte, ""),
                })
        except Exception as e:
            logger.warning(f"Erreur VINCI Autoroutes A8: {e}")
    return alertes


def recuperer_alertes_circulation():
    """Récupère les incidents de circulation via les flux RSS officiels et gratuits
    d'Inforoutes06 (Département des Alpes-Maritimes), zone par zone, complétés par
    VINCI Autoroutes pour l'A8. On ne garde que ce qui concerne l'A8 (les zones
    Inforoutes06 remontent aussi les routes départementales, hors sujet ici)."""
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    alertes = []
    for zone, url in URLS_RSS_CIRCULATION.items():
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            if not r.content.strip():
                continue  # zone sans incident en cours : flux vide, c'est normal
            racine = ET.fromstring(r.content)
            for item in racine.iter("item"):
                titre = nettoyer(html.unescape(item.findtext("title") or ""))
                description_brute = html.unescape(item.findtext("description") or "")
                description = nettoyer(BeautifulSoup(description_brute, "html.parser").get_text(" "))
                guid = (item.findtext("guid") or item.findtext("link") or titre).strip()
                if not titre and not description:
                    continue
                texte_complet = f"{titre} {description}".lower()
                mentionne_a8 = "a8" in texte_complet
                mentionne_corridor_sans_a8 = (
                    ("voie rapide" in texte_complet or "tunnel" in texte_complet)
                    and any(mot in texte_complet for mot in MOTS_CORRIDOR_A8)
                )
                if not (mentionne_a8 or mentionne_corridor_sans_a8):
                    continue  # ni l'A8 ni un tunnel/voie rapide du corridor : hors sujet
                alertes.append({
                    "zone": zone,
                    "titre": titre,
                    "description": description,
                    "guid": guid,
                    "importante": _circulation_est_importante(titre, description),
                })
        except ET.ParseError:
            continue  # flux vide ou mal formé pour cette zone : on ignore silencieusement
        except Exception as e:
            logger.warning(f"Erreur circulation ({zone}): {e}")

    alertes.extend(recuperer_alertes_vinci_a8())
    return alertes


def mettre_a_jour_cache_circulation_si_besoin(force=False):
    global circulation_cache, derniere_maj_circulation, circulation_guids_vus

    if not force and derniere_maj_circulation is not None and (maintenant() - derniere_maj_circulation).total_seconds() < FREQUENCE_CIRCULATION_SECONDES:
        return circulation_cache

    if not circulation_guids_vus and force:
        circulation_guids_vus = charger_circulation_vues()

    try:
        circulation_cache = recuperer_alertes_circulation()
        derniere_maj_circulation = maintenant()
        logger.info(f"Circulation 06: {len(circulation_cache)} incident(s) en cours")
    except Exception as e:
        logger.warning(f"Erreur circulation: {e}")

    return circulation_cache


def envoyer_alertes_circulation(alertes):
    """N'envoie que les incidents importants pas encore vus (nouveaux guid)."""
    global circulation_guids_vus
    nouveaux = False
    for a in alertes:
        if not a["importante"] or a["guid"] in circulation_guids_vus:
            continue
        circulation_guids_vus.add(a["guid"])
        nouveaux = True
        corps = a["description"] or a["titre"]
        envoyer_telegram(
            f"🚧 <b>Circulation — {a['zone']}</b>\n{corps}",
            silencieux=False
        )
    if nouveaux:
        sauver_circulation_vues(circulation_guids_vus)


def commande_circulation(alertes):
    importantes = [a for a in alertes if a["importante"]]
    if not importantes:
        return "🚧 <b>Circulation Monaco → Cannes (A8)</b>\nRien à signaler actuellement."
    corps = "\n\n".join(f"<b>{a['zone']}</b>\n{a['description'] or a['titre']}" for a in importantes[:10])
    return f"🚧 <b>Circulation Monaco → Cannes (A8)</b>\n\n{corps}"


def cle_train(t):
    return f"{t.get('numero')}-{t.get('prevu')}"


def statut_train(t):
    if t.get("annule"):
        return "annule"
    now = maintenant()
    dt = t.get("dt_actuel")
    if dt and now >= dt:
        return "arrive"
    if dt and (dt - now).total_seconds() <= APPROCHE_TRAIN_MINUTES * 60:
        return "approche"
    if t.get("retard", 0) >= RETARD_TRAIN_IMPORTANT_MINUTES:
        return "retard"
    return "prevu"


def icone_train(t):
    s = statut_train(t)
    if s == "annule":
        return "❌"
    if s == "arrive":
        return "✅"
    if s == "approche":
        return "🚄"
    if s == "retard":
        return f"⏰+{t['retard']}"
    return "🟢"


def heure_lisible_train(t):
    if t["prevu"] != "N/A" and t["actuel"] != "N/A" and t["prevu"] != t["actuel"]:
        return f"{t['prevu']}→{t['actuel']}"
    return t["actuel"] if t["actuel"] != "N/A" else t["prevu"]


def est_train_prioritaire(t):
    """Trains TER traités comme prioritaires (étoile + inclus dans le bloc horaire
    normalement réservé aux TGV), sans changer leur libellé affiché 'TER' :
    ex. le TER Marseille Saint-Charles, souvent très fréquenté."""
    provenance = (t.get("provenance") or "").upper()
    return "MARSEILLE" in provenance


def ligne_train(t):
    heure = heure_lisible_train(t)
    type_train = type_court(t.get("type"))
    marqueur = "⭐" if type_train == "TGV" or est_train_prioritaire(t) else "‧"
    provenance = html.escape((t.get("provenance") or "")[:10])
    return f"{marqueur}{heure:<6}{type_train:<5}{provenance:<11}{icone_train(t)}"


def trains_dans_minutes(trains, minutes):
    now = maintenant()
    limite = now + timedelta(minutes=minutes)
    return [t for t in trains if t.get("dt_actuel") and now <= t["dt_actuel"] <= limite]


def envoyer_alertes_trains(trains):
    # Alertes automatiques (approche/arrivé/retard/annulé) : uniquement pour les TGV.
    # Les TER (ZOU!) passent trop souvent (toutes les 10 min environ) pour être
    # signalés individuellement sans spammer le groupe. Ils restent visibles
    # via /sncf et le résumé périodique, juste pas en alerte push.
    trains = [t for t in trains if type_court(t.get("type")) == "TGV"]

    nouveaux_annules, nouvelles_approches, nouveaux_arrives, nouveaux_retards = [], [], [], []

    for t in trains:
        cle = cle_train(t)
        statut = statut_train(t)

        if statut == "annule" and cle not in trains_annules_annonces:
            nouveaux_annules.append(t)
            trains_annules_annonces.add(cle)
            continue

        if statut == "approche" and cle not in trains_approche_annonces:
            nouvelles_approches.append(t)
            trains_approche_annonces.add(cle)

        if statut == "arrive" and cle not in trains_arrives_annonces:
            nouveaux_arrives.append(t)
            trains_arrives_annonces.add(cle)

        if t.get("retard", 0) >= RETARD_TRAIN_IMPORTANT_MINUTES and statut not in ("arrive", "annule"):
            ancien = trains_retard_annonces.get(cle)
            if ancien is None or t["retard"] >= ancien + 10:
                nouveaux_retards.append(t)
                trains_retard_annonces[cle] = t["retard"]

    sections = []

    if nouveaux_annules:
        lignes = "\n".join(f"• {t['prevu']} {type_court(t.get('type'))} {t['numero']}" for t in nouveaux_annules)
        titre = "TRAIN ANNULÉ" if len(nouveaux_annules) == 1 else f"TRAINS ANNULÉS ({len(nouveaux_annules)})"
        sections.append(f"❌ <b>{titre}</b>\n{lignes}")

    if nouvelles_approches:
        lignes = "\n".join(f"• {heure_lisible_train(t)} {type_court(t.get('type'))} {t['numero']}" for t in nouvelles_approches)
        titre = "TRAIN EN APPROCHE" if len(nouvelles_approches) == 1 else f"TRAINS EN APPROCHE ({len(nouvelles_approches)})"
        sections.append(f"🚄 <b>{titre}</b>\n{lignes}")

    if nouveaux_arrives:
        lignes = "\n".join(f"• {t['actuel']} {type_court(t.get('type'))} {t['numero']}" for t in nouveaux_arrives)
        titre = "TRAIN ARRIVÉ" if len(nouveaux_arrives) == 1 else f"TRAINS ARRIVÉS ({len(nouveaux_arrives)})"
        sections.append(f"✅ <b>{titre}</b>\n{lignes}")

    if nouveaux_retards:
        lignes = "\n".join(f"• {heure_lisible_train(t)} {type_court(t.get('type'))} {t['numero']} (+{t['retard']}min)" for t in nouveaux_retards)
        titre = "RETARD TRAIN" if len(nouveaux_retards) == 1 else f"RETARDS TRAINS ({len(nouveaux_retards)})"
        sections.append(f"⏰ <b>{titre}</b>\n{lignes}")

    if sections:
        envoyer_telegram(encadrer_message("\n\n".join(sections)), silencieux=False)


def vols_dans_minutes(vols, minutes):
    now = maintenant()
    limite = now + timedelta(minutes=minutes)
    return [v for v in vols if v.get("dt_actuel") and now <= v["dt_actuel"].astimezone(PARIS) <= limite]


# =========================
# RÉSUMÉ ET ALERTES
# =========================

def nettoyer_caches_si_besoin():
    """Purge quotidienne des sets anti-spam et des entrées live_status trop vieilles,
    pour éviter une fuite mémoire sur une exécution longue."""
    global dernier_nettoyage
    aujourdhui = maintenant().date()
    if dernier_nettoyage == aujourdhui:
        return
    approches_deja_annoncees.clear()
    poses_deja_annonces.clear()
    retards_deja_annonces.clear()
    annules_deja_annonces.clear()
    meilleur_statut_vu.clear()

    trains_approche_annonces.clear()
    trains_arrives_annonces.clear()
    trains_annules_annonces.clear()
    trains_retard_annonces.clear()

    expiration_origine = maintenant() - timedelta(hours=6)
    obsoletes_origine = [k for k, v in origine_cache.items() if v["time"] < expiration_origine]
    for k in obsoletes_origine:
        del origine_cache[k]

    dernier_nettoyage = aujourdhui
    logger.info("Nettoyage quotidien des caches effectué.")


SEPARATEUR_RESUME = "─" * 16
MIN_JOURS_HISTORIQUE = 3  # nombre minimum de jours similaires en historique avant de faire confiance à la moyenne


def niveau_affluence_fixe(nb30):
    """Seuils fixes, utilisés en repli si pas assez d'historique pour comparer intelligemment."""
    if nb30 >= 8: return ("Forte", "🔴")
    if nb30 >= 4: return ("Moyenne", "🟠")
    return ("Calme", "🟢")


def niveau_affluence(nb30):
    """Compare le trafic actuel à la moyenne historique pour le MÊME jour de la semaine
    et la MÊME tranche horaire (ex: samedi 15h vs les samedis précédents à 15h),
    plutôt qu'à un seuil fixe qui ne distingue pas un samedi calme d'un mardi chargé.
    Retombe sur les seuils fixes si l'historique est encore trop pauvre."""
    try:
        maintenant_dt = maintenant()
        jour_semaine = maintenant_dt.strftime("%w")  # 0=dimanche ... 6=samedi
        heure = maintenant_dt.hour
        debut_heure = f"{max(0, heure - 1):02d}:00"
        fin_heure = f"{min(23, heure + 1):02d}:59"
        aujourdhui = maintenant_dt.strftime("%Y-%m-%d")

        conn = sqlite3.connect(DB_FICHIER)
        rows = conn.execute(
            """
            SELECT date, COUNT(*) FROM vols_historique
            WHERE strftime('%w', date) = ?
              AND heure_reelle BETWEEN ? AND ?
              AND date != ?
            GROUP BY date
            """,
            (jour_semaine, debut_heure, fin_heure, aujourdhui),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Erreur calcul affluence historique: {e}")
        rows = []

    if len(rows) < MIN_JOURS_HISTORIQUE:
        return niveau_affluence_fixe(nb30)

    moyenne = sum(r[1] for r in rows) / len(rows)
    if moyenne <= 0:
        return niveau_affluence_fixe(nb30)

    ratio = nb30 / moyenne
    if ratio >= 1.5:
        return ("Forte", "🔴")
    if ratio >= 0.8:
        return ("Moyenne", "🟠")
    return ("Calme", "🟢")


def icone_statut_court(v):
    status = v.get("site_status") or v.get("live_status") or v.get("status") or ""

    if est_pose(status):
        return "✅"
    if est_approche(status):
        return "🛬"
    if est_en_route(status):
        return "✈️"
    if "cancel" in status.lower() or "annul" in status.lower():
        return "❌"
    retard = v.get("retard", 0)
    if retard >= RETARD_IMPORTANT_MINUTES:
        return "⏰"
    if retard >= 10:
        return "🟡"
    return "🟢"


def ligne_vol(v):
    heure = heure_lisible(v)
    ville = html.escape((v['provenance'] or "")[:18])
    ligne = f"{icone_statut_court(v)} {heure} {ville}"
    retard = v.get("retard", 0)
    if retard >= 10:
        ligne += f" +{retard} min"
    return ligne


def bloc_terminal(titre, vols):
    if not vols:
        return f"{titre} (0)\n"
    corps = "\n".join(ligne_vol(v) for v in vols)
    return f"{titre} ({len(vols)})\n\n{corps}\n"


def bloc_trains(trains):
    prioritaires = [t for t in trains if type_court(t.get("type")) == "TGV" or est_train_prioritaire(t)]
    if not prioritaires:
        return ""
    corps = "\n".join(ligne_train(t) for t in prioritaires[:8])
    return f"🚄 <b>TGV (1h)</b>\n\n<code>{corps}</code>\n"


def bloc_etat_voitures():
    """Version compacte de l'état des terminaux, sur une seule ligne — libellés courts
    dédiés (pas label_position) pour garantir que ça tienne même si Babel et Parking
    sont signalés en même temps."""
    LIEU_COURT = {
        ("t1", "reserve"): "T1 BB",
        ("t1", "lineaire"): "T1 POD",
        ("t1", None): "T1",
        ("t2", "parking"): "T2 PK",
        ("t2", "lineaire"): "T2 LIN",
        ("t2", None): "T2",
    }
    data = charger_file_attente()
    parties = []
    for terminal in ("t1", "t2"):
        info = data.get(terminal, {"nombre": 0, "mode": None, "maj": None, "qui": None})
        nb = info.get("nombre", 0)
        mode = info.get("mode")
        mins = minutes_depuis(info.get("maj"))
        if mins is None:
            age = "X"
        elif mins == 0:
            age = "0m"
        else:
            age = f"{mins}m"
        lieu = LIEU_COURT.get((terminal, mode), terminal.upper())
        valeur = "⚡" if nb == "TIRE" else (
            "3/4+" if isinstance(nb, str) and nb.startswith("Q") and nb[1:].isdigit() else f"<b>{nb}</b>"
        )
        parties.append(f"{lieu}:{valeur}({age})")
    return "🚕 " + "·".join(parties)


def creer_resume(vols, trains=None):
    d_heure = vols_dans_minutes(vols, 60)
    t1_heure = [v for v in d_heure if v["terminal"] == "1"]
    t2_heure = [v for v in d_heure if v["terminal"] == "2"]
    heure_resume_str = maintenant().strftime("%H:%M")

    blocs = [
        "<u><b>FLIGHT ALERT</b></u>\n🕒 " + heure_resume_str,
        ligne_repos_resume(),
    ]
    if evenement_du_jour_cache:
        blocs.append(evenement_du_jour_cache)
    if matchs_concerts_cache:
        blocs.append(matchs_concerts_cache)
    blocs += [
        bloc_etat_voitures(),
        bloc_terminal("🔵 T1 · 1h", t1_heure).strip(),
        bloc_terminal("🔴 T2 · 1h", t2_heure).strip(),
    ]

    if trains:
        trains_60 = trains_dans_minutes(trains, 60)
        bloc = bloc_trains(trains_60)
        if bloc:
            blocs.append(bloc.strip())

    return f"\n{SEPARATEUR_RESUME}\n".join(blocs)


def initialiser_sans_spam(vols):
    for v in vols:
        cle = cle_vol(v)
        status = v.get("site_status") or v.get("live_status") or v.get("status")
        if est_annule(status):
            annules_deja_annonces.add(cle)
        if est_approche(status):
            approches_deja_annoncees.add(cle)
        if est_pose(status):
            poses_deja_annonces.add(cle)
        if v["retard"] >= RETARD_IMPORTANT_MINUTES:
            retards_deja_annonces[cle] = v["retard"]


def initialiser_sans_spam_trains(trains):
    for t in trains:
        cle = cle_train(t)
        statut = statut_train(t)
        if statut == "approche":
            trains_approche_annonces.add(cle)
        if statut == "arrive":
            trains_arrives_annonces.add(cle)
        if statut == "annule":
            trains_annules_annonces.add(cle)
        if t.get("retard", 0) >= RETARD_TRAIN_IMPORTANT_MINUTES:
            trains_retard_annonces[cle] = t["retard"]


def init_db():
    try:
        conn = sqlite3.connect(DB_FICHIER)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vols_historique (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                heure_prevue TEXT,
                heure_reelle TEXT,
                numero TEXT,
                compagnie TEXT,
                provenance TEXT,
                terminal TEXT,
                retard INTEGER,
                statut TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Erreur init DB historique: {e}")


def enregistrer_vol_historique(v):
    try:
        conn = sqlite3.connect(DB_FICHIER)
        conn.execute(
            "INSERT INTO vols_historique "
            "(date, heure_prevue, heure_reelle, numero, compagnie, provenance, terminal, retard, statut) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                maintenant().strftime("%Y-%m-%d"),
                v.get("prevu"), v.get("actuel"), v.get("numero"),
                v.get("compagnie"), v.get("provenance"), v.get("terminal"),
                v.get("retard", 0), v.get("site_status") or v.get("status"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Erreur écriture DB historique: {e}")


def est_annule(status):
    s = (status or "").lower()
    return "cancel" in s or "annul" in s


def encadrer_message(texte, caractere="─", longueur=16):
    """Ajoute une ligne de séparation en bas seulement (plus de ligne du haut),
    sans bordures latérales (qui se déformeraient à cause des emojis)."""
    ligne = caractere * longueur
    return f"{texte}\n{ligne}"


def ligne_alerte_vol(v, avec_retard=False):
    heure = heure_lisible(v)
    ville = html.escape((v['provenance'] or "")[:18])
    ligne = f"• {heure} {ville}"
    if avec_retard:
        ligne += f" (+{v['retard']}min)"
    return ligne


def info_voitures_courte(terminal_num):
    """Résumé court du dernier signalement pour ce terminal, avec précision d'emplacement
    (Babel/Linéaire/Parking), à afficher à côté de T1/T2 dans les alertes."""
    data = charger_file_attente()
    info = data.get(f"t{terminal_num}", {"nombre": 0, "mode": None, "maj": None, "qui": None})
    if not info.get("maj"):
        return "(pas signalé)"
    nb = info.get("nombre", 0)
    mode = info.get("mode")
    mins = minutes_depuis(info.get("maj"))
    age = "à l'instant" if mins == 0 else f"{mins} min"
    lieu = {"reserve": "Babel", "lineaire": "Linéaire", "parking": "Parking"}.get(mode)
    prefixe = f"({lieu}) " if lieu else ""
    if nb == "TIRE":
        return f"{prefixe}⚡ rythme soutenu ({age})"
    return f"{prefixe}· {nb} ({age})"


def regrouper_par_terminal(liste_vols, formatter):
    """Regroupe une liste de vols par terminal (T1 puis T2), comme dans le résumé principal."""
    t1 = [v for v in liste_vols if str(v.get("terminal")) == "1"]
    t2 = [v for v in liste_vols if str(v.get("terminal")) == "2"]
    blocs = []
    if t1:
        blocs.append("🔵 <b>T1</b>\n" + "\n".join(formatter(v) for v in t1))
    if t2:
        blocs.append("🔴 <b>T2</b>\n" + "\n".join(formatter(v) for v in t2))
    return "\n".join(blocs)


def charger_abonnements_vol():
    try:
        with open(ABONNEMENTS_VOL_FICHIER, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sauver_abonnements_vol(data):
    try:
        with open(ABONNEMENTS_VOL_FICHIER, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Erreur sauvegarde abonnements vol: {e}")


def commande_suivi(numero, user_id, nom):
    numero = (numero or "").upper().replace(" ", "")
    if not numero:
        return "Précise un numéro de vol, ex : <code>/suivi EJU1618</code>"
    data = charger_abonnements_vol()
    abonnes = data.setdefault(numero, [])
    if not any(a["user_id"] == user_id for a in abonnes):
        abonnes.append({"user_id": user_id, "nom": nom})
    sauver_abonnements_vol(data)
    return (
        f"🔔 Suivi activé pour le vol <b>{numero}</b>.\n"
        f"Je te tague dans le groupe dès qu'il passe en approche, puis quand il atterrit."
    )


def notifier_abonnes_vol(vols_evenement, evenement_label, emoji):
    """Envoie un message qui tague (mention cliquable) chaque personne abonnée à un vol
    qui vient de passer en approche ou de se poser. L'abonnement est retiré après l'atterrissage."""
    abonnements = charger_abonnements_vol()
    if not abonnements:
        return
    modifie = False
    for v in vols_evenement:
        numero = (v.get("numero") or "").upper().replace(" ", "")
        abonnes = abonnements.get(numero)
        if not abonnes:
            continue
        tags = " ".join(f'<a href="tg://user?id={a["user_id"]}">{html.escape(a["nom"])}</a>' for a in abonnes)
        envoyer_telegram(
            f"{emoji} {tags} ton vol <b>{numero}</b> {evenement_label} !",
            silencieux=False
        )
        if evenement_label == "vient de se poser":
            del abonnements[numero]
            modifie = True
    if modifie:
        sauver_abonnements_vol(abonnements)


def envoyer_alertes(vols):
    nouveaux_annules, nouvelles_approches, nouveaux_poses, nouveaux_retards = [], [], [], []

    for v in vols:
        cle = cle_vol(v)
        status = v.get("site_status") or v.get("live_status") or v.get("status")

        if est_annule(status) and cle not in annules_deja_annonces:
            nouveaux_annules.append(v)
            annules_deja_annonces.add(cle)
            continue

        if est_approche(status) and cle not in approches_deja_annoncees:
            nouvelles_approches.append(v)
            approches_deja_annoncees.add(cle)

        if est_pose(status) and cle not in poses_deja_annonces:
            nouveaux_poses.append(v)
            poses_deja_annonces.add(cle)
            enregistrer_vol_historique(v)

        if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(status):
            ancien = retards_deja_annonces.get(cle)
            if ancien is None or v["retard"] >= ancien + 10:
                nouveaux_retards.append(v)
                retards_deja_annonces[cle] = v["retard"]

    logger.info(
        f"envoyer_alertes: {len(vols)} vols analysés -> "
        f"nouveaux: annules={len(nouveaux_annules)} approches={len(nouvelles_approches)} "
        f"poses={len(nouveaux_poses)} retards={len(nouveaux_retards)} | "
        f"tailles mémoire anti-spam: approches_vus={len(approches_deja_annoncees)} poses_vus={len(poses_deja_annonces)}"
    )

    sections = []

    if nouveaux_annules:
        lignes = regrouper_par_terminal(
            nouveaux_annules,
            lambda v: f"• {v['prevu']} {html.escape((v['provenance'] or '')[:18])}"
        )
        titre = "VOL ANNULÉ" if len(nouveaux_annules) == 1 else f"VOLS ANNULÉS ({len(nouveaux_annules)})"
        sections.append(f"❌ <b>{titre}</b>\n{lignes}")

    if nouvelles_approches:
        lignes = regrouper_par_terminal(nouvelles_approches, ligne_alerte_vol)
        titre = "EN APPROCHE" if len(nouvelles_approches) == 1 else f"EN APPROCHE ({len(nouvelles_approches)})"
        sections.append(f"🛬 <b>{titre}</b>\n{lignes}")

    if nouveaux_poses:
        lignes = regrouper_par_terminal(nouveaux_poses, ligne_alerte_vol)
        titre = "POSÉ" if len(nouveaux_poses) == 1 else f"POSÉS ({len(nouveaux_poses)})"
        sections.append(f"✅ <b>{titre}</b>\n{lignes}")

    if nouveaux_retards:
        lignes = regrouper_par_terminal(nouveaux_retards, lambda v: ligne_alerte_vol(v, avec_retard=True))
        titre = "RETARD" if len(nouveaux_retards) == 1 else f"RETARDS ({len(nouveaux_retards)})"
        sections.append(f"⏰ <b>{titre}</b>\n{lignes}")

    if sections:
        envoyer_telegram(encadrer_message("\n\n".join(sections)), silencieux=False)

    if nouvelles_approches:
        notifier_abonnes_vol(nouvelles_approches, "est en approche", "🔔")
    if nouveaux_poses:
        notifier_abonnes_vol(nouveaux_poses, "vient de se poser", "🔔")


# =========================
# CYCLE DE REPOS TAXIS (4 couleurs, 2 jours chacune, rotation continue)
# Ordre du cycle : Jaune -> Bleu -> Blanc -> Rouge -> (retour à Jaune...)
# Calculé par calcul (pas de grille à retaper), à partir d'un point de repère fixe.
# =========================

CYCLE_REPOS = [
    ("Rouge", 1), ("Rouge", 2),
    ("Jaune", 1), ("Jaune", 2),
    ("Bleu", 1), ("Bleu", 2),
    ("Blanc", 1), ("Blanc", 2),
]
REPOS_DATE_REFERENCE = datetime(2026, 6, 15).date()  # Rouge, 1er jour de repos (confirmé)

EMOJI_COULEUR_REPOS = {
    "Jaune": "🟡",
    "Bleu": "🔵",
    "Blanc": "⚪",
    "Rouge": "🔴",
}


def couleur_repos(pour_date=None):
    """Renvoie (couleur, jour) — jour vaut 1 ou 2 — pour la date donnée (aujourd'hui par défaut)."""
    d = pour_date or maintenant().date()
    diff = (d - REPOS_DATE_REFERENCE).days
    idx = diff % 8
    return CYCLE_REPOS[idx]


def ligne_repos_resume():
    couleur, _ = couleur_repos()
    emoji = EMOJI_COULEUR_REPOS.get(couleur, "")
    return f"Repos : {couleur} {emoji}"


def commande_repos():
    couleur, jour = couleur_repos()
    emoji = EMOJI_COULEUR_REPOS.get(couleur, "")
    if jour == 1:
        suite = "(1er jour, encore repos demain)"
    else:
        suite = "(2ème et dernier jour)"
    return f"{emoji} {couleur} en repos aujourd'hui {suite}"


# =========================
# ÉVÉNEMENTS DU JOUR (Cannes / Monaco en priorité, Nice en repli)
# Scanné une fois par jour (pas à chaque résumé) et mis en cache, puisqu'un
# événement ne change pas dans la journée — inutile de re-scraper toutes les 30 min.
# =========================

URL_EVENEMENTS_CANNES = "https://www.palaisdesfestivals.com/agenda/professionnel/"
URL_EVENEMENTS_MONACO = "https://www.grimaldiforum.com/fr/agenda/page"
URL_EVENEMENTS_NICE = "https://www.meet-in-nicecotedazur.com/agenda-pro/"

MOIS_FR = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12, "déc": 12, "janv": 1, "fev": 2, "fév": 2,
    "fevr": 2, "févr": 2, "avr": 4, "juil": 7,
}

evenement_du_jour_cache = None  # texte prêt à insérer dans le résumé, ou None si rien aujourd'hui
dernier_scan_evenements = None  # date du dernier scan


def _normaliser_mois(mot):
    m = unicodedata.normalize("NFKD", (mot or "").lower().rstrip("."))
    m = "".join(c for c in m if not unicodedata.combining(c))
    return MOIS_FR.get(m)


def construire_date_evenement(jour, mois_nom, annee=None):
    """Construit une date à partir d'un jour + nom de mois français, en devinant
    l'année si elle n'est pas fournie (déduite par rapport à aujourd'hui)."""
    mois = _normaliser_mois(mois_nom)
    if not mois:
        return None
    try:
        jour = int(jour)
    except (TypeError, ValueError):
        return None
    aujourdhui = maintenant().date()
    if annee is not None:
        try:
            return datetime(int(annee), mois, jour).date()
        except ValueError:
            return None
    for candidate_annee in (aujourdhui.year, aujourdhui.year + 1):
        try:
            candidate = datetime(candidate_annee, mois, jour).date()
        except ValueError:
            continue
        if (aujourdhui - candidate).days <= 60:
            return candidate
    return None


def extraire_evenement_cannes(texte_brut):
    """Extrait titre + dates d'un texte de carte événement Cannes, du type
    'Cannes Yachting Festival Du 8 au 13 Septembre' ou 'NRJ Music Awards Le Vendredi 23 Oct. à 21:00'."""
    t = nettoyer(texte_brut)
    for pattern, kind in (
        (r"Du\s+(\d{1,2})\s+([A-Za-zéûôîâ]+)\s+au\s+(\d{1,2})\s+([A-Za-zéûôîâ]+)", "cross"),
        (r"Du\s+(\d{1,2})\s+au\s+(\d{1,2})\s+([A-Za-zéûôîâ]+)", "same"),
        (r"Le\s+\w+\s+(\d{1,2})\s+([A-Za-zéûôîâ]+)\.?", "single"),
    ):
        m = re.search(pattern, t, re.IGNORECASE)
        if not m:
            continue
        titre = t[:m.start()].strip(" -–:")
        if len(titre) < 3:
            continue
        if kind == "cross":
            d1 = construire_date_evenement(m.group(1), m.group(2))
            d2 = construire_date_evenement(m.group(3), m.group(4))
        elif kind == "same":
            mois = m.group(3)
            d1 = construire_date_evenement(m.group(1), mois)
            d2 = construire_date_evenement(m.group(2), mois)
        else:
            d1 = construire_date_evenement(m.group(1), m.group(2))
            d2 = d1
        if not d1 or not d2:
            continue
        return {"titre": titre, "debut": d1, "fin": d2}
    return None


def recuperer_evenements_cannes():
    evenements = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
        r = requests.get(URL_EVENEMENTS_CANNES, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a"):
            evt = extraire_evenement_cannes(a.get_text(" ", strip=True))
            if evt:
                evenements.append(evt)
        logger.info(f"Événements Cannes: {len(evenements)} trouvés")
    except Exception as e:
        logger.warning(f"Erreur récupération événements Cannes: {e}")
    return evenements


def extraire_dates_monaco(texte_brut):
    """Extrait (date_debut, date_fin) d'un texte de carte événement Monaco, du type
    'mer. 1 juillet - dim. 6 septembre 2026', 'sam. 11 - dim. 12 juillet 2026'
    ou 'sam. 12 septembre 2026'."""
    t = nettoyer(texte_brut)
    m = re.search(
        r"(\d{1,2})\s+([A-Za-zéûôîâ]+)\s*-\s*[a-zéû]+\.?\s*(\d{1,2})\s+([A-Za-zéûôîâ]+)\s+(\d{4})",
        t, re.IGNORECASE
    )
    if m:
        d1 = construire_date_evenement(m.group(1), m.group(2), m.group(5))
        d2 = construire_date_evenement(m.group(3), m.group(4), m.group(5))
        return d1, d2
    m = re.search(
        r"(\d{1,2})\s*-\s*[a-zéû]+\.?\s*(\d{1,2})\s+([A-Za-zéûôîâ]+)\s+(\d{4})",
        t, re.IGNORECASE
    )
    if m:
        mois, annee = m.group(3), m.group(4)
        d1 = construire_date_evenement(m.group(1), mois, annee)
        d2 = construire_date_evenement(m.group(2), mois, annee)
        return d1, d2
    m = re.search(r"(\d{1,2})\s+([A-Za-zéûôîâ]+)\s+(\d{4})", t, re.IGNORECASE)
    if m:
        d = construire_date_evenement(m.group(1), m.group(2), m.group(3))
        return d, d
    return None, None


def recuperer_evenements_monaco():
    evenements = []
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    try:
        for page in (1, 2, 3):
            data = {"event-filter[dates]": "", "event-filter[search]": "", "event-filter[index]": str(page)}
            r = requests.post(URL_EVENEMENTS_MONACO, headers=headers, data=data, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            cartes = soup.find_all("a", class_="landing__events__item")
            if not cartes:
                break
            for carte in cartes:
                nom_div = carte.find("div", class_="landing__events__item__name")
                dates_div = carte.find("div", class_="landing__events__item__dates")
                if not nom_div or not dates_div:
                    continue
                titre = nettoyer(nom_div.get_text(strip=True))
                d1, d2 = extraire_dates_monaco(dates_div.get_text(strip=True))
                if not d1 or not d2:
                    continue
                evenements.append({"titre": titre, "debut": d1, "fin": d2})
        logger.info(f"Événements Monaco: {len(evenements)} trouvés")
    except Exception as e:
        logger.warning(f"Erreur récupération événements Monaco: {e}")
    return evenements


def extraire_evenement_nice(texte_brut):
    """Extrait titre + dates d'un texte de carte événement Nice, du type
    'NOM DE L'ÉVÉNEMENT 27 sept. 01 oct. 2026'."""
    t = nettoyer(texte_brut)
    m = re.search(
        r"(\d{1,2})\s+([A-Za-zéûôîâ]+)\.?\s+(\d{1,2})\s+([A-Za-zéûôîâ]+)\.?\s+(\d{4})",
        t, re.IGNORECASE
    )
    if not m:
        return None
    titre = t[:m.start()].strip(" -–:")
    if len(titre) < 3:
        return None
    d1 = construire_date_evenement(m.group(1), m.group(2), m.group(5))
    d2 = construire_date_evenement(m.group(3), m.group(4), m.group(5))
    if not d1 or not d2:
        return None
    return {"titre": titre, "debut": d1, "fin": d2}


def recuperer_evenements_nice():
    evenements = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
        r = requests.get(URL_EVENEMENTS_NICE, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a"):
            evt = extraire_evenement_nice(a.get_text(" ", strip=True))
            if evt:
                evenements.append(evt)
        logger.info(f"Événements Nice: {len(evenements)} trouvés")
    except Exception as e:
        logger.warning(f"Erreur récupération événements Nice: {e}")
    return evenements


def _evenement_est_aujourdhui(evt):
    aujourdhui = maintenant().date()
    return evt["debut"] <= aujourdhui <= evt["fin"]


# Ces événements restent affichés tout du long (pas seulement les 3 premiers jours),
# vu leur importance pour l'activité : on matche sur un mot-clé du titre.
EVENEMENTS_TOUJOURS_VISIBLES = ("mipim", "lion", "festival de cannes", "grand prix de monaco")


def _evenement_visible(evt):
    """Un événement n'intéresse les chauffeurs que les 3 premiers jours (jours d'arrivée
    des participants) — sauf les quelques rendez-vous majeurs qui restent affichés
    jusqu'à leur fin."""
    if not _evenement_est_aujourdhui(evt):
        return False
    if any(mot in evt["titre"].lower() for mot in EVENEMENTS_TOUJOURS_VISIBLES):
        return True
    jours_ecoules = (maintenant().date() - evt["debut"]).days
    return jours_ecoules < 3


def _formater_contenu_evenement(evt, ville):
    titre = evt["titre"]
    if evt["debut"] == evt["fin"]:
        return titre + " (" + ville + ")"
    fin_str = evt["fin"].strftime("%d/%m")
    return titre + " (" + ville + ", jusqu'au " + fin_str + ")"


def mettre_a_jour_evenements_si_besoin(force=False):
    """Scanne Cannes/Monaco (priorité) puis Nice (repli) une fois par jour,
    et met en cache le résultat pour tous les résumés de la journée."""
    global evenement_du_jour_cache, dernier_scan_evenements
    aujourdhui = maintenant().date()
    if not force and dernier_scan_evenements == aujourdhui:
        return

    try:
        cannes = [e for e in recuperer_evenements_cannes() if _evenement_visible(e)]
        monaco = [e for e in recuperer_evenements_monaco() if _evenement_visible(e)]
        prioritaires = [(e, "Cannes") for e in cannes] + [(e, "Monaco") for e in monaco]

        if prioritaires:
            contenus = [_formater_contenu_evenement(e, ville) for e, ville in prioritaires[:2]]
        else:
            nice = [e for e in recuperer_evenements_nice() if _evenement_visible(e)]
            contenus = [_formater_contenu_evenement(e, "Nice") for e in nice[:2]]

        if contenus:
            lignes_italique = [f"<i>{c}</i>" for c in contenus]
            evenement_du_jour_cache = "Événements : " + "\n".join(lignes_italique)
        else:
            evenement_du_jour_cache = None
        logger.info(f"Événement(s) du jour retenu(s): {evenement_du_jour_cache!r}")
    except Exception as e:
        logger.warning(f"Erreur mise à jour événements du jour: {e}")

    dernier_scan_evenements = aujourdhui


# =========================
# MATCHS & CONCERTS (Allianz Riviera, Palais Nikaïa) — même logique que les
# événements Cannes/Monaco/Nice ci-dessus : un scan par jour, mis en cache.
# =========================

URL_NIKAIA_PROGRAMMATION = "https://www.nikaia.fr/programmation"

matchs_concerts_cache = None
dernier_scan_matchs_concerts = None

# Mots qui excluent un événement Nikaïa de "Matchs & concerts" (salons professionnels,
# foires, expositions... pas des concerts/spectacles).
MOTS_EXCLUS_NIKAIA = ("salon", "foire", "exposition", "congrès", "congres", "brocante")

_LIGNES_A_IGNORER_CALENDRIER = ("nice", "allianz riviera", "tout sur le match", "compte-rendu")


def saison_ogcnice_actuelle():
    """Devine la saison OGC Nice en cours (ex: '2026-2027') à partir du mois. La saison de
    Ligue 1 se termine fin mai et la suivante commence mi-juillet ; à partir de juillet on
    bascule donc déjà sur la saison à venir — comme le fait le menu du site du club lui-même.
    Détection automatique pour ne jamais avoir à mettre à jour cette URL à la main."""
    n = maintenant()
    if n.month >= 7:
        return f"{n.year}-{n.year + 1}"
    return f"{n.year - 1}-{n.year}"


def recuperer_match_allianz_riviera_du_jour():
    """Cherche dans le calendrier officiel OGC Nice s'il y a un match à domicile
    (Allianz Riviera) aujourd'hui, et renvoie le nom de l'adversaire, ou None sinon."""
    saison = saison_ogcnice_actuelle()
    url = f"https://www.ogcnice.com/fr/calendrier/f/{saison}/equipe-pro"
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"Erreur calendrier OGC Nice ({saison}): {e}")
        return None

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        lignes = [nettoyer(l) for l in soup.get_text("\n").split("\n") if nettoyer(l)]
        aujourdhui = maintenant().date()
        jours_semaine = r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
        regex_date = re.compile(rf"^(?:{jours_semaine})\s+(\d{{1,2}})\s+([A-Za-zéûôîâ]+)$", re.IGNORECASE)

        for i, ligne in enumerate(lignes):
            m = regex_date.match(ligne)
            if not m:
                continue
            date_match = construire_date_evenement(m.group(1), m.group(2))
            if date_match != aujourdhui:
                continue
            fenetre = lignes[i + 1:i + 15]
            if not any("allianz riviera" in l.lower() for l in fenetre):
                continue  # match du jour, mais à l'extérieur : pas notre affaire
            for l in fenetre:
                bl = l.lower()
                if bl in _LIGNES_A_IGNORER_CALENDRIER:
                    continue
                if re.fullmatch(r"\d{1,2}", l):
                    continue  # jour du mois redondant
                if re.fullmatch(r"\d+(\s*\(\d+\))?", l):
                    continue  # score
                if _normaliser_mois(l):
                    continue  # mois abrégé redondant
                return l  # premier nom d'équipe restant = l'adversaire
            return "adversaire à confirmer"
        return None
    except Exception as e:
        logger.warning(f"Erreur analyse calendrier OGC Nice: {e}")
        return None


def recuperer_concert_nikaia_du_jour():
    """Cherche sur la page officielle du Palais Nikaïa s'il y a un concert/spectacle
    aujourd'hui, et renvoie son titre, ou None sinon. Exclut salons/foires/expositions,
    qui ne sont pas des concerts/spectacles."""
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/15.0"}
    try:
        r = requests.get(URL_NIKAIA_PROGRAMMATION, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"Erreur programmation Nikaïa: {e}")
        return None

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        lignes = [nettoyer(l) for l in soup.get_text("\n").split("\n") if nettoyer(l)]
        aujourdhui = maintenant().date()

        regex_simple = re.compile(
            r"^Le\s+\w+\s+(\d{1,2})(?:er)?\s+([A-Za-zéûôîâ]+)\.?\s+(\d{4})$", re.IGNORECASE
        )
        regex_plage = re.compile(
            r"^Du\s+(\d{1,2})(?:er)?\s+([A-Za-zéûôîâ]+)\.?\s+au\s+(\d{1,2})(?:er)?\s+([A-Za-zéûôîâ]+)\.?\s+(\d{4})$",
            re.IGNORECASE
        )

        for i, ligne in enumerate(lignes):
            m_simple = regex_simple.match(ligne)
            m_plage = regex_plage.match(ligne) if not m_simple else None
            if m_simple:
                d = construire_date_evenement(m_simple.group(1), m_simple.group(2), m_simple.group(3))
                dans_periode = (d == aujourdhui)
            elif m_plage:
                d1 = construire_date_evenement(m_plage.group(1), m_plage.group(2), m_plage.group(5))
                d2 = construire_date_evenement(m_plage.group(3), m_plage.group(4), m_plage.group(5))
                dans_periode = bool(d1 and d2 and d1 <= aujourdhui <= d2)
            else:
                continue
            if not dans_periode or i == 0:
                continue
            titre = lignes[i - 1]
            if any(mot in titre.lower() for mot in MOTS_EXCLUS_NIKAIA):
                continue
            return titre
        return None
    except Exception as e:
        logger.warning(f"Erreur analyse programmation Nikaïa: {e}")
        return None


def mettre_a_jour_matchs_concerts_si_besoin(force=False):
    """Scanne une fois par jour le calendrier OGC Nice (Allianz Riviera) et la
    programmation du Palais Nikaïa, et met en cache le résultat pour les résumés du jour."""
    global matchs_concerts_cache, dernier_scan_matchs_concerts
    aujourdhui = maintenant().date()
    if not force and dernier_scan_matchs_concerts == aujourdhui:
        return

    try:
        lignes = []
        adversaire = recuperer_match_allianz_riviera_du_jour()
        if adversaire:
            lignes.append(f"<i>OGC Nice - {adversaire} (Allianz Riviera)</i>")
        concert = recuperer_concert_nikaia_du_jour()
        if concert:
            lignes.append(f"<i>{concert} (Palais Nikaïa)</i>")

        matchs_concerts_cache = "Matchs & Concerts\n" + "\n".join(lignes) if lignes else None
        logger.info(f"Matchs & concerts du jour retenu(s): {matchs_concerts_cache!r}")
    except Exception as e:
        logger.warning(f"Erreur mise à jour matchs & concerts: {e}")

    dernier_scan_matchs_concerts = aujourdhui


# =========================
# COMMANDES TELEGRAM (getUpdates = API Telegram, gratuite et illimitée)
# Ces commandes ne lisent QUE vols_cache : elles ne déclenchent JAMAIS
# d'appel RapidAPI, donc aucun impact sur le quota mensuel.
# =========================

def recuperer_updates_telegram():
    global dernier_update_id
    params = {"timeout": 20}  # long-polling : la requête reste ouverte jusqu'à 20s, réponse immédiate dès qu'il y a du nouveau
    if dernier_update_id is not None:
        params["offset"] = dernier_update_id + 1
    try:
        r = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params=params, timeout=25)
        if not r.ok:
            if r.status_code == 409:
                logger.error(
                    "getUpdates 409 Conflict : une AUTRE instance du bot utilise le même token en même temps "
                    "(vérifier qu'il n'y a qu'un seul déploiement actif sur Railway)."
                )
            else:
                logger.error(f"Erreur getUpdates Telegram ({r.status_code}): {r.text[:200]}")
            return []
        return r.json().get("result", [])
    except Exception as e:
        logger.error(f"Erreur getUpdates Telegram: {e}")
        return []


def supprimer_message_telegram(chat_id, message_id):
    """Supprime un message (ex: le 'Signaler' envoyé automatiquement en tapant le bouton fixe),
    pour garder le groupe propre. Nécessite que le bot soit admin avec le droit de supprimer."""
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/deleteMessage",
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur suppression message Telegram: {e}")


def repondre_telegram(chat_id, message, silencieux=True):
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={
                "chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True,
                "disable_notification": silencieux,
                "reply_markup": json.dumps(clavier_permanent()),  # re-rattache le bouton fixe à chaque envoi
            },
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Erreur réponse commande Telegram: {e}")


# =========================
# SIGNALEMENT RAPIDE PAR BOUTONS (2 taps, sans clavier à sortir)
# =========================

LOC_INFOS = {
    "t1_babel": ("t1", "reserve"),
    "t1_lineaire": ("t1", "lineaire"),
    "t2_parking": ("t2", "parking"),
    "t2_lineaire": ("t2", "lineaire"),
    "gare": ("gare", None),
}
LOC_LABELS = {
    "t1_babel": "🅿️ T1 Babel",
    "t1_lineaire": "🚕 T1 Linéaire",
    "t2_parking": "🅿️ T2 Parking",
    "t2_lineaire": "🚕 T2 Linéaire",
    "gare": "🚉 Gare",
}
NOMBRES_RAPIDES = [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30]
NOMBRES_RAPIDES_GARE = [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20]  # la gare ne dépasse jamais 15-20 voitures
attente_nombre_personnalise = {}  # (chat_id, user_id) -> (terminal, mode)

NOMBRES_PAX_VOLEE = [0, 5, 10, 15, 20, 30, 40, 50, 60, 80, 100]
attente_pax_volee = {}  # (chat_id, user_id) -> (terminal_code, prompt_id, qui)

# ---- Auto-suppression des menus "Choisis l'emplacement" jamais utilisés ----
MENU_TIMEOUT_SECONDES = int(os.getenv("MENU_TIMEOUT_SECONDES", "10"))
menus_en_attente = {}  # message_id -> {"chat_id": ..., "envoye": datetime}

# ---- Alerte "volée" (beaucoup de monde, plus assez de taxis) ----
DUREE_VOLEE_MINUTES = int(os.getenv("DUREE_VOLEE_MINUTES", "25"))
volee_active = {}  # "v1"/"v2"/"vgare" -> {"debut": datetime, "message_id": int}
LABEL_VOLEE = {"v1": "TERMINAL 1", "v2": "TERMINAL 2", "vgare": "LA GARE"}


def clavier_emplacements():
    return {"inline_keyboard": [
        [{"text": "🅿️ T1 Babel", "callback_data": "loc:t1_babel"},
         {"text": "🚕 T1 Linéaire", "callback_data": "loc:t1_lineaire"}],
        [{"text": "🅿️ T2 Parking", "callback_data": "loc:t2_parking"},
         {"text": "🚕 T2 Linéaire", "callback_data": "loc:t2_lineaire"}],
        [{"text": "🚉 Gare", "callback_data": "loc:gare"}],
        [{"text": "🚨 VOLÉE T1", "callback_data": "volee:v1"},
         {"text": "🚨 VOLÉE T2", "callback_data": "volee:v2"}],
        [{"text": "🚨 VOLÉE GARE", "callback_data": "volee:vgare"}],
    ]}


def clavier_confirmation_volee(terminal_code):
    return {"inline_keyboard": [[
        {"text": "✅ Confirmer", "callback_data": f"voleeconfirm:{terminal_code}"},
        {"text": "❌ Annuler", "callback_data": "loc:retour"},
    ]]}


def clavier_pax_volee(terminal_code):
    """Clavier affiché après confirmation, pour préciser environ combien de passagers
    attendent — cette info est incluse dans le message d'alerte final."""
    lignes, ligne = [], []
    for i, n in enumerate(NOMBRES_PAX_VOLEE, start=1):
        ligne.append({"text": str(n), "callback_data": f"vpaxcnt:{terminal_code}:{n}"})
        if i % 5 == 0:
            lignes.append(ligne)
            ligne = []
    if ligne:
        lignes.append(ligne)
    lignes.append([{"text": "✏️ Autre nombre", "callback_data": f"vpaxcustom:{terminal_code}"}])
    lignes.append([{"text": "🌐 Info extérieure (pas de nb)", "callback_data": f"vpaxexterieur:{terminal_code}"}])
    lignes.append([{"text": "❌ Annuler", "callback_data": f"voleeannulerpax:{terminal_code}"}])
    return {"inline_keyboard": lignes}


def clavier_annulation_volee(terminal_code):
    return {"inline_keyboard": [[
        {"text": "🛑 Annuler l'alerte", "callback_data": f"voleeannuler:{terminal_code}"},
    ]]}


def clavier_nombres(loc):
    lignes, ligne = [], []
    liste_nombres = NOMBRES_RAPIDES_GARE if loc == "gare" else NOMBRES_RAPIDES
    for i, n in enumerate(liste_nombres, start=1):
        ligne.append({"text": str(n), "callback_data": f"cnt:{loc}:{n}"})
        if i % 5 == 0:
            lignes.append(ligne)
            ligne = []
    if ligne:
        lignes.append(ligne)
    if loc == "t2_lineaire":
        lignes.append([{"text": "3/4", "callback_data": f"cnt:{loc}:3/4"},
                        {"text": "A4 (½ parking)", "callback_data": f"cnt:{loc}:A4"},
                        {"text": "FULL", "callback_data": f"cnt:{loc}:FULL"}])
        lignes.append([{"text": "🟧 Quasi full", "callback_data": f"quasifull:{loc}"}])
    if loc == "t1_babel":
        lignes.append([{"text": "FULL", "callback_data": f"cnt:{loc}:FULL"}])
        lignes.append([{"text": "⬇️ Descente", "callback_data": f"descente:{loc}"}])
        lignes.append([{"text": "🟧 La quille descend", "callback_data": "quilledescend"}])
    if loc == "t1_lineaire":
        lignes.append([{"text": "🟧 La quille monte", "callback_data": "quillemonte"}])
    if loc == "t2_parking":
        lignes.append([{"text": "FULL", "callback_data": f"cnt:{loc}:FULL"}])
    if loc == "gare":
        lignes.append([{"text": "FULL", "callback_data": f"cnt:{loc}:FULL"}])
    lignes.append([{"text": "✏️ Autre nombre", "callback_data": f"custom:{loc}"}])
    lignes.append([{"text": "⬅️ Retour", "callback_data": "loc:retour"}])
    return {"inline_keyboard": lignes}


def texte_descente_babel(qui, nombre_restant):
    if nombre_restant > 0:
        return (
            f"⬇️ <b>{qui} descend de Babel pour le linéaire</b>\n"
            f"Il reste <b>{nombre_restant}</b> taxi(s) à Babel."
        )
    return f"⬇️ <b>{qui} descend de Babel</b>\nBabel est vide !"


def definir_quille_t1(position, qui):
    """position: 'babel' (à Babel) ou 'lineaire' (au linéaire). La quille matérialise
    l'ordre de passage entre les deux emplacements de T1."""
    data = charger_file_attente()
    data["quille_t1"] = {"position": position, "maj": maintenant().isoformat(), "qui": qui}
    sauver_file_attente(data)


def texte_quille(position, qui):
    if position == "lineaire":
        return f"🟧 <b>{qui} vient de prendre la quille à Babel et la descend au linéaire.</b>"
    return f"🟧 <b>{qui} vient de prendre la quille au linéaire et la remonte à Babel.</b>"


def label_position_quille(position):
    return "🅿️ Babel" if position == "babel" else "🚕 Linéaire"


def clavier_descente(loc):
    """Clavier affiché après un tap sur '⬇️ Descente' : demande combien de taxis
    restent à Babel une fois que la personne en est descendue."""
    lignes, ligne = [], []
    for i, n in enumerate(NOMBRES_RAPIDES, start=1):
        ligne.append({"text": str(n), "callback_data": f"desccnt:{loc}:{n}"})
        if i % 5 == 0:
            lignes.append(ligne)
            ligne = []
    if ligne:
        lignes.append(ligne)
    lignes.append([{"text": "✏️ Autre nombre", "callback_data": f"desccustom:{loc}"}])
    lignes.append([{"text": "⬅️ Retour", "callback_data": f"descretour:{loc}"}])
    return {"inline_keyboard": lignes}


LIEU_COURT_BOUTON = {
    ("t1", "reserve"): "T1 BB",
    ("t1", "lineaire"): "T1 LIN",
    ("t1", None): "T1",
    ("t2", "parking"): "T2 PK",
    ("t2", "lineaire"): "T2 LIN",
    ("t2", None): "T2",
}


MAX_LONGUEUR_BOUTON_ETAT = 48  # au-delà, ça risque de passer sur 2 lignes sur petit écran


def texte_bouton_etat():
    """Texte compact, une seule ligne, sans HTML — pour le bouton d'état permanent
    affiché au-dessus de 'Signaler'. Toujours à jour car reconstruit à chaque envoi."""
    data = charger_file_attente()
    parties = []
    for terminal in ("t1", "t2"):
        info = data.get(terminal) or {"nombre": 0, "mode": None}
        nb = info.get("nombre", 0)
        mode = info.get("mode")
        lieu = LIEU_COURT_BOUTON.get((terminal, mode), terminal.upper())
        valeur = "⚡" if nb == "TIRE" else ("3/4+" if isinstance(nb, str) and nb.startswith("Q") and nb[1:].isdigit() else str(nb))
        age = minutes_depuis(info.get("maj"))
        age_txt = f"({min(age, 99)}m)" if age is not None else ""
        parties.append(f"{lieu}: {valeur}{age_txt}")
    gare = data.get("gare") or {"nombre": 0}
    age_gare = minutes_depuis(gare.get("maj"))
    age_gare_txt = f"({min(age_gare, 99)}m)" if age_gare is not None else ""
    parties.append(f"GARE: {gare.get('nombre', 0)}{age_gare_txt}")

    texte = " · ".join(parties)

    quille = data.get("quille_t1")
    if quille:
        pos = "LIN" if quille.get("position") == "lineaire" else "BB"
        texte_avec_quille = texte + f" · Q: {pos}"
        # On n'ajoute la quille que si ça ne fait pas déborder sur une 2e ligne.
        if len(texte_avec_quille) <= MAX_LONGUEUR_BOUTON_ETAT:
            texte = texte_avec_quille
    return texte


def clavier_permanent():
    """Reconstruit le clavier fixe à chaque envoi : la 1ère ligne affiche l'état en
    direct (jamais figée), la 2e est le bouton Signaler habituel."""
    return {
        "keyboard": [[{"text": texte_bouton_etat()}], [{"text": "SIGNALER"}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def envoyer_clavier_permanent(chat_id):
    """Affiche le bouton '🚖 Signaler' en permanence en bas de l'écran (remplace le clavier),
    pour que les chauffeurs n'aient jamais besoin de taper une commande."""
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": "🚖 Appuie sur le bouton ci-dessous pour signaler des voitures, à tout moment.",
                "disable_notification": True,
                "reply_markup": json.dumps(clavier_permanent()),
            },
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Erreur envoi clavier permanent: {e}")


def envoyer_telegram_clavier(chat_id, texte, clavier):
    try:
        r = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={"chat_id": chat_id, "text": texte, "parse_mode": "HTML", "reply_markup": json.dumps(clavier)},
            timeout=15,
        )
        if r.ok:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Erreur envoi clavier Telegram: {e}")
    return None


def editer_message_telegram(chat_id, message_id, texte, clavier=None):
    try:
        data = {"chat_id": chat_id, "message_id": message_id, "text": texte, "parse_mode": "HTML"}
        if clavier is not None:
            data["reply_markup"] = json.dumps(clavier)
        requests.post(f"{TELEGRAM_API_URL}/editMessageText", data=data, timeout=15)
    except Exception as e:
        logger.error(f"Erreur édition message Telegram: {e}")


def repondre_callback(callback_id, texte=None):
    try:
        data = {"callback_query_id": callback_id}
        if texte:
            data["text"] = texte
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery", data=data, timeout=10)
    except Exception as e:
        logger.error(f"Erreur answerCallbackQuery: {e}")


def envoyer_demande_nombre(chat_id, label, question=None):
    """Demande le nombre en réattachant explicitement le clavier fixe (au lieu de force_reply,
    qui remplace temporairement le clavier fixe et le fait parfois disparaître pour de bon
    une fois le message supprimé). Le message sera supprimé juste après traitement."""
    texte = question or f"✏️ Tape le nombre pour {label} et envoie-le."
    try:
        r = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": texte,
                "reply_markup": json.dumps(clavier_permanent()),
            },
            timeout=15,
        )
        if r.ok:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Erreur envoi demande nombre: {e}")
    return None


def texte_alerte_volee(terminal_code, qui=None, nb_pax=None, source_externe=False):
    label = LABEL_VOLEE.get(terminal_code, terminal_code.upper())
    seuil = 10 if terminal_code == "vgare" else 20
    beaucoup_de_monde = nb_pax is None or nb_pax >= seuil
    corps = "🚨🚨🚨 <b>ALERTE VOLÉE</b> 🚨🚨🚨\n\n"
    if beaucoup_de_monde:
        corps += f"<b>BEAUCOUP DE MONDE À {label}</b>\n"
    else:
        corps += f"<b>VOLÉE À {label}</b>\n"
    if nb_pax is not None:
        corps += f"👥 Environ <b>{nb_pax}</b> passagers en attente\n"
    elif source_externe:
        corps += "ℹ️ Info remontée d'un autre groupe, pas de nombre précis de passagers.\n"
    corps += (
        "Besoin de renfort dès que possible !\n\n"
        f"⏱️ Expire automatiquement dans {DUREE_VOLEE_MINUTES} min si non annulée."
    )
    if qui:
        corps += f"\n<i>Déclenché par {qui}</i>"
    # Pas d'encadrement ici : les 🚨 doivent être les tout premiers caractères,
    # sinon l'aperçu épinglé en haut du chat n'affiche que les tirets du cadre.
    return corps


def epingler_message(chat_id, message_id):
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/pinChatMessage",
            data={"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur épinglage message: {e}")


def desepingler_message(chat_id, message_id):
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/unpinChatMessage",
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Erreur désépinglage message: {e}")


def envoyer_alerte_volee(chat_id, terminal_code, qui=None, nb_pax=None, source_externe=False):
    """Envoie le message d'alerte voyant, épinglé, avec un bouton pour l'annuler manuellement."""
    try:
        r = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": texte_alerte_volee(terminal_code, qui, nb_pax, source_externe),
                "parse_mode": "HTML",
                "disable_notification": False,  # notification normale, pas silencieuse
                "reply_markup": json.dumps(clavier_annulation_volee(terminal_code)),
            },
            timeout=15,
        )
        if r.ok:
            message_id = r.json().get("result", {}).get("message_id")
            if message_id:
                epingler_message(chat_id, message_id)
            return message_id
    except Exception as e:
        logger.error(f"Erreur envoi alerte volée: {e}")
    return None


def verifier_expiration_volees():
    """Fait expirer automatiquement les alertes volée après DUREE_VOLEE_MINUTES."""
    a_retirer = [
        code for code, info in volee_active.items()
        if (maintenant() - info["debut"]).total_seconds() >= DUREE_VOLEE_MINUTES * 60
    ]
    for code in a_retirer:
        info = volee_active.pop(code)
        editer_message_telegram(
            TELEGRAM_CHAT_ID, info["message_id"],
            f"✅ Alerte VOLÉE {code.upper()} terminée (expirée après {DUREE_VOLEE_MINUTES} min)."
        )
        desepingler_message(TELEGRAM_CHAT_ID, info["message_id"])


def verifier_expiration_menus():
    """Supprime les menus 'Choisis l'emplacement' jamais utilisés après MENU_TIMEOUT_SECONDES."""
    a_retirer = [
        mid for mid, info in menus_en_attente.items()
        if (maintenant() - info["envoye"]).total_seconds() >= MENU_TIMEOUT_SECONDES
    ]
    for mid in a_retirer:
        info = menus_en_attente.pop(mid)
        supprimer_message_telegram(info["chat_id"], mid)


def traiter_callback(callback):
    """Gère les taps sur les boutons du clavier de signalement rapide."""
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    callback_id = callback["id"]
    qui = (callback.get("from") or {}).get("first_name", "quelqu'un")
    user_id_stats = (callback.get("from") or {}).get("id")

    # Le menu a été touché : on annule sa suppression automatique programmée.
    menus_en_attente.pop(message_id, None)

    if data == "loc:retour":
        repondre_callback(callback_id)
        editer_message_telegram(chat_id, message_id, "🚖 Choisis l'emplacement :", clavier_emplacements())
        return

    if data.startswith("voleeannuler:"):
        terminal_code = data.split(":", 1)[1]
        if terminal_code in volee_active:
            info = volee_active.pop(terminal_code)
            repondre_callback(callback_id, "Alerte annulée")
            editer_message_telegram(
                chat_id, info["message_id"],
                f"✅ Alerte VOLÉE {terminal_code.upper()} terminée (annulée manuellement)."
            )
            desepingler_message(chat_id, info["message_id"])
        else:
            repondre_callback(callback_id)
        return

    if data.startswith("voleeconfirm:"):
        terminal_code = data.split(":", 1)[1]
        if terminal_code in volee_active:
            # Déjà active : silence total, on ne renvoie rien dans le groupe.
            repondre_callback(callback_id)
            supprimer_message_telegram(chat_id, message_id)
            return
        repondre_callback(callback_id)
        editer_message_telegram(
            chat_id, message_id,
            "🔢 Environ combien de passagers attendent ?",
            clavier_pax_volee(terminal_code)
        )
        return

    if data.startswith("voleeannulerpax:"):
        repondre_callback(callback_id, "Annulé")
        supprimer_message_telegram(chat_id, message_id)
        return

    if data.startswith("vpaxcnt:"):
        _, terminal_code, nombre_str = data.split(":", 2)
        try:
            nb_pax = int(nombre_str)
        except ValueError:
            repondre_callback(callback_id)
            return
        if terminal_code in volee_active:
            repondre_callback(callback_id)
            supprimer_message_telegram(chat_id, message_id)
            return
        repondre_callback(callback_id, "Alerte envoyée")
        supprimer_message_telegram(chat_id, message_id)
        nouveau_message_id = envoyer_alerte_volee(chat_id, terminal_code, qui, nb_pax)
        if nouveau_message_id:
            volee_active[terminal_code] = {"debut": maintenant(), "message_id": nouveau_message_id}
        return

    if data.startswith("vpaxexterieur:"):
        terminal_code = data.split(":", 1)[1]
        if terminal_code in volee_active:
            repondre_callback(callback_id)
            supprimer_message_telegram(chat_id, message_id)
            return
        repondre_callback(callback_id, "Alerte envoyée")
        supprimer_message_telegram(chat_id, message_id)
        nouveau_message_id = envoyer_alerte_volee(chat_id, terminal_code, qui, nb_pax=None, source_externe=True)
        if nouveau_message_id:
            volee_active[terminal_code] = {"debut": maintenant(), "message_id": nouveau_message_id}
        return

    if data.startswith("vpaxcustom:"):
        terminal_code = data.split(":", 1)[1]
        user_id = (callback.get("from") or {}).get("id")
        repondre_callback(callback_id)
        supprimer_message_telegram(chat_id, message_id)
        prompt_id = envoyer_demande_nombre(
            chat_id, None,
            question="✏️ Environ combien de passagers ? Tape le nombre et envoie-le."
        )
        attente_pax_volee[(chat_id, user_id)] = (terminal_code, prompt_id, qui)
        return

    if data.startswith("volee:"):
        terminal_code = data.split(":", 1)[1]
        repondre_callback(callback_id)
        label = LABEL_VOLEE.get(terminal_code, terminal_code.upper())
        editer_message_telegram(
            chat_id, message_id,
            f"⚠️ Confirmer l'alerte VOLÉE {label} ? Tout le monde va être prévenu.",
            clavier_confirmation_volee(terminal_code)
        )
        return

    if data.startswith("loc:"):
        loc = data.split(":", 1)[1]
        if loc not in LOC_INFOS:
            repondre_callback(callback_id)
            return
        repondre_callback(callback_id)
        label = LOC_LABELS.get(loc, loc)
        editer_message_telegram(chat_id, message_id, f"🔢 Combien de voitures à {label} ?", clavier_nombres(loc))
        return

    if data.startswith("quasifullcnt:"):
        _, loc, n_str = data.split(":", 2)
        terminal, mode = LOC_INFOS.get(loc, (None, None))
        try:
            n = int(n_str)
        except ValueError:
            repondre_callback(callback_id)
            return
        if terminal:
            definir_position(terminal, f"Q{n}", mode, qui)
            enregistrer_annonce(user_id_stats, qui)
            repondre_callback(callback_id, "Enregistré ✅")
            texte_confirmation = (
                f"✅ {label_position(terminal, mode)} : <b>Quasi full</b> — encore {n} place(s)"
                f"\n<i>Signalé par {qui}</i>"
            )
            supprimer_message_telegram(chat_id, message_id)
            repondre_telegram(chat_id, f"🟧 {texte_confirmation}", silencieux=False)
        else:
            repondre_callback(callback_id)
        return

    if data.startswith("quasifull:"):
        loc = data.split(":", 1)[1]
        repondre_callback(callback_id)
        lignes = [[{"text": str(n), "callback_data": f"quasifullcnt:{loc}:{n}"} for n in range(1, 5)],
                  [{"text": str(n), "callback_data": f"quasifullcnt:{loc}:{n}"} for n in range(5, 9)]]
        editer_message_telegram(
            chat_id, message_id,
            "🔢 Combien de places restantes ?",
            {"inline_keyboard": lignes}
        )
        return

    if data.startswith("cnt:"):
        _, loc, nombre_str = data.split(":", 2)
        if nombre_str in ("A4", "FULL", "3/4"):
            nombre = nombre_str
        else:
            try:
                nombre = int(nombre_str)
            except ValueError:
                repondre_callback(callback_id)
                return
        terminal, mode = LOC_INFOS.get(loc, (None, None))
        if terminal:
            definir_position(terminal, nombre, mode, qui)
            enregistrer_annonce(user_id_stats, qui)
            repondre_callback(callback_id, "Enregistré ✅")
            if nombre == "A4":
                texte_confirmation = f"✅ {label_position(terminal, mode)} : <b>A4</b> (½ parking)"
            elif nombre == "3/4":
                texte_confirmation = f"✅ {label_position(terminal, mode)} : <b>3/4</b> (linéaire presque plein)"
            elif nombre == "FULL":
                if loc == "t1_babel":
                    precision = "Babel plein"
                elif loc == "t2_lineaire":
                    precision = "Fin linéaire T2"
                elif loc == "t2_parking":
                    precision = "Parking T2 plein"
                elif loc == "gare":
                    precision = "Gare pleine"
                else:
                    precision = "complet"
                texte_confirmation = f"✅ {label_position(terminal, mode)} : <b>FULL</b> ({precision})"
            else:
                texte_confirmation = f"✅ {label_position(terminal, mode)} : <b>{nombre}</b> voitures"
            texte_confirmation += f"\n<i>Signalé par {qui}</i>"
            supprimer_message_telegram(chat_id, message_id)
            repondre_telegram(chat_id, f"🟧 {texte_confirmation}", silencieux=False)
        else:
            repondre_callback(callback_id)
        return

    if data.startswith("custom:"):
        loc = data.split(":", 1)[1]
        terminal, mode = LOC_INFOS.get(loc, (None, None))
        if not terminal:
            repondre_callback(callback_id)
            return
        user_id = (callback.get("from") or {}).get("id")
        repondre_callback(callback_id)
        label = LOC_LABELS.get(loc, loc)
        supprimer_message_telegram(chat_id, message_id)
        prompt_id = envoyer_demande_nombre(chat_id, label)
        attente_nombre_personnalise[(chat_id, user_id)] = (terminal, mode, prompt_id, "normal")
        return

    if data in ("quilledescend", "quillemonte"):
        position = "lineaire" if data == "quilledescend" else "babel"
        qui = (callback.get("from") or {}).get("first_name", "quelqu'un")
        definir_quille_t1(position, qui)
        if data == "quilledescend":
            definir_position("t1", 0, "reserve", qui, sync_quille=False)  # Babel se vide forcément quand la quille descend
        else:
            definir_position("t1", "FULL", "lineaire", qui, sync_quille=False)  # le linéaire est forcément plein quand la quille remonte
            definir_position("t1", 1, "reserve", qui)  # la personne qui remonte la quille est maintenant à Babel
        repondre_callback(callback_id, "Enregistré ✅")
        repondre_telegram(chat_id, texte_quille(position, qui), silencieux=False)
        return

    if data.startswith("descente:"):
        loc = data.split(":", 1)[1]
        repondre_callback(callback_id)
        editer_message_telegram(
            chat_id, message_id,
            "🔢 Combien reste-t-il de taxis à Babel après ta descente ?",
            clavier_descente(loc)
        )
        return

    if data.startswith("descretour:"):
        loc = data.split(":", 1)[1]
        repondre_callback(callback_id)
        label = LOC_LABELS.get(loc, loc)
        editer_message_telegram(chat_id, message_id, f"🔢 Combien de voitures à {label} ?", clavier_nombres(loc))
        return

    if data.startswith("desccnt:"):
        _, loc, nombre_str = data.split(":", 2)
        try:
            nombre = int(nombre_str)
        except ValueError:
            repondre_callback(callback_id)
            return
        terminal, mode = LOC_INFOS.get(loc, (None, None))
        if terminal:
            definir_position(terminal, nombre, mode, qui)
            enregistrer_annonce(user_id_stats, qui)
            repondre_callback(callback_id, "Enregistré ✅")
            supprimer_message_telegram(chat_id, message_id)
            repondre_telegram(chat_id, texte_descente_babel(qui, nombre), silencieux=False)
        else:
            repondre_callback(callback_id)
        return

    if data.startswith("desccustom:"):
        loc = data.split(":", 1)[1]
        terminal, mode = LOC_INFOS.get(loc, (None, None))
        if not terminal:
            repondre_callback(callback_id)
            return
        user_id = (callback.get("from") or {}).get("id")
        repondre_callback(callback_id)
        supprimer_message_telegram(chat_id, message_id)
        prompt_id = envoyer_demande_nombre(
            chat_id, None,
            question="✏️ Combien reste-t-il de taxis à Babel ? Tape le nombre et envoie-le."
        )
        attente_nombre_personnalise[(chat_id, user_id)] = (terminal, mode, prompt_id, "descente")
        return

    repondre_callback(callback_id)


def commande_prochain(vols):
    a_venir = [
        v for v in vols
        if v.get("dt_actuel") and v["dt_actuel"].astimezone(PARIS) >= maintenant()
        and not est_annule(v.get("site_status") or v.get("live_status") or v.get("status"))
    ]
    a_venir.sort(key=lambda v: v["dt_actuel"])
    if not a_venir:
        return "Aucun vol à venir dans les données actuelles (cache site, sans appel API)."
    v = a_venir[0]
    return (
        "🛬 <b>Prochain vol</b>\n"
        f"{heure_lisible(v)} · {v['provenance']} · {v['compagnie']}\n"
        f"{emoji_terminal(v['terminal'])} · {statut_lisible(v)}"
    )


def vols_atterris_recemment(vols, minutes=15):
    now = maintenant()
    limite = now - timedelta(minutes=minutes)
    resultat = []
    for v in vols:
        statut = v.get("site_status") or v.get("live_status") or v.get("status") or ""
        if not est_pose(statut):
            continue
        dt_prevu = v.get("dt_prevu")
        actuel_str = v.get("actuel")
        if not dt_prevu or not actuel_str or actuel_str == "N/A":
            continue
        try:
            h, m = map(int, actuel_str.split(":"))
        except ValueError:
            continue
        # Heure réelle d'atterrissage reconstruite depuis "actuel" (même jour que dt_prevu) :
        # on ne peut pas se fier à dt_actuel ici, un vol posé en avance a son retard plafonné
        # à 0 par minutes_retard, ce qui laisse dt_actuel sur l'heure prévue (donc au futur).
        dt_atterrissage = dt_prevu.replace(hour=h, minute=m)
        ecart_jours = round((dt_atterrissage - dt_prevu).total_seconds() / 86400)
        if abs(ecart_jours) >= 1:
            dt_atterrissage -= timedelta(days=ecart_jours)
        if limite <= dt_atterrissage <= now:
            resultat.append(v)
    return resultat


def commande_terminal(vols, terminal, minutes=60):
    filtres = [v for v in vols_dans_minutes(vols, minutes) if v["terminal"] == terminal]
    recents = [v for v in vols_atterris_recemment(vols, 30) if v["terminal"] == terminal]
    duree_label = f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}min"

    if not filtres and not recents:
        return f"Aucun vol prévu en Terminal {terminal} dans les {duree_label} (données cache)."

    sections = []
    if recents:
        corps_recents = "\n".join(ligne_vol(v) for v in recents[:10])
        sections.append(f"🛬 <b>Posés (30 min)</b>\n<code>{corps_recents}</code>")
    if filtres:
        corps = "\n".join(ligne_vol(v) for v in filtres[:20])
        sections.append(f"📍 <b>Terminal {terminal}</b> ({duree_label})\n<code>{corps}</code>")
    return "\n\n".join(sections)


def commande_vol(vols, numero):
    numero = (numero or "").upper().replace(" ", "")
    for v in vols:
        if (v.get("numero") or "").upper().replace(" ", "") == numero:
            return (
                f"✈️ <b>{v['numero']}</b> · {v['compagnie']}\n"
                f"De {v['provenance']} · {emoji_terminal(v['terminal'])}\n"
                f"{heure_lisible(v)} · {statut_lisible(v)}"
            )
    return f"Vol {numero} introuvable dans les données actuelles."


def est_admin_telegram(chat_id, user_id):
    """Vérifie en direct auprès de Telegram si user_id est administrateur (ou créateur)
    du groupe chat_id — pas de liste figée à maintenir, ça suit les changements faits
    dans les paramètres du groupe."""
    try:
        r = requests.get(
            f"{TELEGRAM_API_URL}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=10,
        )
        statut = r.json().get("result", {}).get("status")
        return statut in ("administrator", "creator")
    except Exception as e:
        logger.warning(f"Erreur vérification admin: {e}")
        return False


def texte_annonce(message, qui):
    bordure = "🚨" + "━" * 29 + "🚨"
    return (
        f"{bordure}\n"
        f"   <b>ANNONCE IMPORTANTE</b>\n"
        f"{bordure}\n\n"
        f"{html.escape(message)}\n\n"
        f"— {html.escape(qui)}"
    )


def commande_sncf(trains):
    if not SNCF_API_TOKEN:
        return "🚄 Module trains pas encore activé (token SNCF manquant)."
    filtres = trains_dans_minutes(trains, 60)
    if not filtres:
        return "Aucun train prévu à Nice-Ville dans l'heure (données cache)."
    corps = "\n".join(ligne_train(t) for t in filtres[:10])
    return f"🚄 <b>Trains Nice-Ville</b> (1h)\n<code>{corps}</code>"


# =========================
# COMPTEUR DE VOITURES PAR TERMINAL
# Les chauffeurs signalent l'état en tapant simplement un message,
# ex: "8pk t2", "30 parking t2", "T1 15", "3 linéaire t1"
# Le nombre représente le TOTAL de voitures sur ce terminal (linéaire + débordement).
# =========================

def charger_file_attente():
    try:
        with open(FILE_FICHIER, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"t1": {"nombre": 0, "mode": None, "maj": None, "qui": None},
                "t2": {"nombre": 0, "mode": None, "maj": None, "qui": None},
                "gare": {"nombre": 0, "mode": None, "maj": None, "qui": None}}


def sauver_file_attente(data):
    try:
        with open(FILE_FICHIER, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Erreur sauvegarde file d'attente: {e}")


def definir_position(terminal, nombre, mode, qui, sync_quille=True):
    data = charger_file_attente()
    data[terminal] = {"nombre": nombre, "mode": mode, "maj": maintenant().isoformat(), "qui": qui}
    sauver_file_attente(data)
    if sync_quille and terminal == "t1":
        if mode == "lineaire":
            # Voitures signalées au linéaire T1 : la quille est forcément descendue là-bas.
            definir_quille_t1("lineaire", qui)
        elif mode == "reserve":
            # Voitures (ou FULL) signalées à Babel : la quille est forcément encore là-bas.
            definir_quille_t1("babel", qui)
    return data[terminal]


def minutes_depuis(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        return max(0, int((maintenant() - dt).total_seconds() // 60))
    except Exception:
        return None


def parser_position(texte):
    """Extrait (terminal, nombre, mode) d'un message libre du type
    '8pk t2', '30 parking t2', '10t1', '10 t1', '3 t2', '4 t2', '3 linéaire t1'.
    - mode 'parking'/'reserve' = débordement (linéaire plein, nombre = total du terminal)
    - mode 'lineaire' = pas encore plein — c'est aussi le mode par défaut d'un simple
      'nombre + terminal' sans autre précision (ex: '10t1', '10 t1', '3 t2', '4 t2'),
      collé ou avec espace, car c'est ce que les chauffeurs veulent dire en pratique.
    Retourne None si pas de nombre, 'ambigu' si 'linéaire' cité sans terminal précisé."""
    t = texte.lower().strip()

    # Extraire le terminal en premier et le retirer du texte, pour ne pas confondre
    # le "1" de "T1" avec le nombre de voitures. (?<![a-z]) au lieu de \b devant le "t"
    # pour aussi reconnaître les formes collées comme "10t1" (chiffre directement suivi
    # de "t1", sans frontière de mot classique entre un chiffre et une lettre).
    terminal = None
    m = re.search(r"(?<![a-z])t ?1\b", t)
    if m:
        terminal = "t1"
        t_sans_terminal = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"(?<![a-z])t ?2\b", t)
        if m:
            terminal = "t2"
            t_sans_terminal = t[:m.start()] + " " + t[m.end():]
        else:
            t_sans_terminal = t

    nums = re.findall(r"\d+", t_sans_terminal)
    if not nums:
        return None
    nombre = int(nums[0])

    if "reserve" in t or "réserve" in t or "babel" in t:
        return (terminal or "t1", nombre, "reserve")
    if "parking" in t or "pk" in t:
        return (terminal or "t2", nombre, "parking")
    if "lineaire" in t or "linéaire" in t or "podium" in t:
        if terminal is None:
            return "ambigu"
        return (terminal, nombre, "lineaire")
    if terminal is not None:
        return (terminal, nombre, "lineaire")
    return None


def detecter_format_court_lineaire(texte):
    """Détecte les formats compacts comme '3vt2', '2vt1' (nombre+v+t+terminal collés),
    toujours interprétés comme linéaire. Nécessite une correspondance EXACTE du message
    entier (pas juste une sous-chaîne), pour ne jamais se déclencher dans une longue phrase."""
    t = texte.lower().strip()
    m = re.fullmatch(r"(\d+)\s*v\s*t\s*([12])", t)
    if not m:
        return None
    nombre = int(m.group(1))
    terminal = f"t{m.group(2)}"
    return (terminal, nombre, "lineaire")


def detecter_gare(texte):
    """Détecte 'gare 5', '5 gare', '5v gare', 'gare 5v' etc. comme signalement du nombre
    de voitures à la Gare de Nice, et 'gare full'/'gare pleine'/'gare plein' pour FULL —
    un seul emplacement, pas de sous-mode."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    if re.fullmatch(r"gare\s*(full|plein|pleine)", t) or re.fullmatch(r"(full|plein|pleine)\s*gare", t):
        return ("gare", "FULL", None)
    m = re.fullmatch(r"gare\s*(\d+)\s*v?", t) or re.fullmatch(r"(\d+)\s*v?\s*gare", t)
    if not m:
        return None
    nombre = int(m.group(1))
    return ("gare", nombre, None)


def detecter_special_t2(texte):
    """Détecte 't2 a4', 'a4', 't2 3/4', '3/4', 't2 3 quarts', '3 quarts',
    't2 fin linéaire', 't2 linéaire full' etc. — les valeurs spéciales du linéaire
    T2 (A4 = ½ parking, 3/4 = presque plein, FULL = complet), normalement accessibles
    seulement par bouton. Toujours interprété comme T2, seul terminal concerné.
    Pour FULL/fin linéaire, 't2' doit être mentionné explicitement (sinon ambigu
    avec le FULL du Babel T1)."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))  # accents retirés : linéaire -> lineaire
    t2_mentionne = bool(re.search(r"(?<![a-z])t ?2", t))
    t_sans_terminal = re.sub(r"(?<![a-z])t ?2", "", t).strip()

    if re.fullmatch(r"a ?4", t_sans_terminal):
        return ("t2", "A4", "lineaire")
    if re.fullmatch(r"3\s*/\s*4|3\s*quarts?|trois\s*quarts?", t_sans_terminal):
        return ("t2", "3/4", "lineaire")
    if "parking" in t_sans_terminal or re.search(r"\bpk\b", t_sans_terminal):
        return None  # jamais de confusion avec le parking, même si "full" ou "linéaire" traîne dans le message

    if t2_mentionne and (re.search(r"fin\s*lineaire|lineaire\s*full|full\s*lineaire", t_sans_terminal) or t_sans_terminal == "full"):
        return ("t2", "FULL", "lineaire")
    return None


def detecter_full_parking_t2(texte):
    """Détecte le parking T2 complet : 'parking full', 'pk t2 plein', 't2 pk plein',
    'parking t2 plein', 't2 parking plein', 't2 parking full', 'full parking', 'plein pk', etc.
    Exige le mot 'parking'/'pk' EN PLUS de 'full'/'plein' — pour ne jamais entrer en collision
    avec 't2 full' tout seul, qui reste réservé au linéaire T2 (déjà utilisé par les chauffeurs).
    Toujours T2, seul terminal avec un mode parking."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    a_parking = bool(re.search(r"\bparking\b|\bpk\b", t))
    a_plein = bool(re.search(r"\bfull\b|\bpleine?\b", t))
    if a_parking and a_plein:
        return ("t2", "FULL", "parking")
    return None


def detecter_places_restantes_t2(texte):
    """Détecte 'T2 linéaire reste 3 places', 't2 reste 3 place', 'reste 2 place t2', etc.
    Toujours T2 linéaire (jamais parking), avec le nombre de places encore libres —
    équivalent texte libre du bouton 'Quasi full'."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    if not re.search(r"(?<![a-z])t ?2", t):
        return None
    if "parking" in t or re.search(r"(?<![a-z])pk(?![a-z])", t):
        return None  # ça concerne le parking, pas le linéaire
    m = re.search(r"rest\w*\D*(\d+)\s*plac", t) or re.search(r"(\d+)\s*plac\w*\D*rest", t)
    if not m:
        return None
    n = int(m.group(1))
    if not (1 <= n <= 8):
        return None
    return ("t2", f"Q{n}", "lineaire")


def detecter_bb_babel(texte):
    """Détecte 'bb23', '23bb', 'bb 23', '23 bb' (avec ou sans espace) comme signalement
    du nombre de voitures à Babel (T1) — 'bb' est utilisé par les chauffeurs comme
    raccourci pour Babel. Toujours T1, puisque Babel n'existe qu'à ce terminal."""
    t = texte.lower().strip()
    m = re.fullmatch(r"bb\s*(\d+)", t) or re.fullmatch(r"(\d+)\s*bb", t)
    if not m:
        return None
    nombre = int(m.group(1))
    return ("t1", nombre, "reserve")


def texte_maj_pax_volee(terminal_code, nb_pax, qui):
    label = LABEL_VOLEE.get(terminal_code, terminal_code.upper())
    return f"🚨 <b>{label}</b> : il reste encore <b>{nb_pax}</b> passager(s) en attente\n<i>Mis à jour par {qui}</i>"


def detecter_maj_pax_volee(texte):
    """Quand une volée est déjà active, un simple nombre ('10') ou 'terminal + nombre'
    ('t2 10', '10 t2') met à jour le nombre de passagers restants, sans redéclencher
    une nouvelle alerte. Ne s'applique que s'il y a au moins une volée active — sinon
    'kt2 10' garde son sens normal (10 voitures au linéaire T2)."""
    if not volee_active:
        return None
    t = texte.lower().strip()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))

    m_seul = re.fullmatch(r"(\d+)\s*(?:pax)?", t)
    if m_seul:
        nb = int(m_seul.group(1))
        if len(volee_active) == 1:
            return (next(iter(volee_active)), nb)
        return "ambigu"

    terminal_code = None
    if re.search(r"(?<![a-z])t ?1", t):
        terminal_code = "v1"
    elif re.search(r"(?<![a-z])t ?2", t):
        terminal_code = "v2"
    elif "gare" in t:
        terminal_code = "vgare"
    if not terminal_code or terminal_code not in volee_active:
        return None
    m = re.search(r"\d+", t)
    if not m:
        return None
    return (terminal_code, int(m.group()))


def detecter_volee_pax(texte):
    """Détecte 'v1 10', 'v1 10pax', 'v2 15 pax' etc. comme déclenchement DIRECT
    d'une alerte VOLÉE avec un nombre de passagers, sans repasser par l'écran
    de confirmation — taper un nombre explicite est déjà une action volontaire."""
    t = texte.lower().strip()
    m = re.fullmatch(r"v([12])\s+(\d+)\s*(?:pax)?", t)
    if not m:
        return None
    terminal_code = f"v{m.group(1)}"
    nb_pax = int(m.group(2))
    return (terminal_code, nb_pax)


def detecter_ca_tire(texte):
    """Détecte 'ça tire t1' / 'ça tire t2' (avec ou sans accent/espace),
    pour signaler un rythme soutenu sans donner de nombre précis."""
    t = texte.lower().strip()
    if "tire" not in t:
        return None
    if re.search(r"\bt ?1\b", t):
        return "t1"
    if re.search(r"\bt ?2\b", t):
        return "t2"
    return None


def label_position(terminal, mode):
    if terminal == "gare":
        return "🚉 Gare"
    if terminal == "t1":
        if mode == "reserve":
            return "🅿️ T1 (Babel)"
        if mode == "lineaire":
            return "🚕 T1 (linéaire)"
        return "🚕 T1"
    else:
        if mode == "parking":
            return "🅿️ T2 (parking)"
        if mode == "lineaire":
            return "🚕 T2 (linéaire)"
        return "🚕 T2"


def commande_etat_file():
    data = charger_file_attente()
    lignes = ["🚖 <b>État des terminaux</b>\n"]
    for terminal in ("t1", "t2", "gare"):
        info = data.get(terminal, {"nombre": 0, "mode": None, "maj": None, "qui": None})
        nb = info.get("nombre", 0)
        mode = info.get("mode")
        mins = minutes_depuis(info.get("maj"))
        if mins is None:
            age = "jamais signalé"
        elif mins == 0:
            age = "à l'instant"
        else:
            age = f"il y a {mins} min"
        if nb == "TIRE":
            lignes.append(f"{label_position(terminal, mode)} : ⚡ <b>Rythme soutenu</b> ({age})")
        elif isinstance(nb, str) and nb.startswith("Q") and nb[1:].isdigit():
            lignes.append(f"{label_position(terminal, mode)} : <b>3/4+</b> ({age})")
        else:
            lignes.append(f"{label_position(terminal, mode)} : <b>{nb}</b> ({age})")

    quille = data.get("quille_t1")
    if quille:
        mins_quille = minutes_depuis(quille.get("maj"))
        age_quille = "jamais signalé" if mins_quille is None else (
            "à l'instant" if mins_quille == 0 else f"il y a {mins_quille} min"
        )
        lignes.append(
            f"\n🟧 Quille T1 : <b>{label_position_quille(quille.get('position'))}</b> "
            f"({age_quille}, {quille.get('qui') or '?'})"
        )
    return "\n".join(lignes)


def commande_vider_file():
    data = {"t1": {"nombre": 0, "mode": None, "maj": maintenant().isoformat(), "qui": "reset"},
            "t2": {"nombre": 0, "mode": None, "maj": maintenant().isoformat(), "qui": "reset"},
            "gare": {"nombre": 0, "mode": None, "maj": maintenant().isoformat(), "qui": "reset"}}
    sauver_file_attente(data)
    return "🔄 Les compteurs T1, T2 et Gare ont été remis à zéro."


# =========================
# TOP DES ANNONCES (qui signale le plus de voitures dans la journée)
# =========================

def charger_stats_jour():
    try:
        with open(STATS_FICHIER, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    jour_actuel = maintenant().strftime("%Y-%m-%d")
    if data.get("jour") != jour_actuel:
        data = {"jour": jour_actuel, "compteurs": {}}
    return data


def sauver_stats_jour(data):
    try:
        with open(STATS_FICHIER, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Erreur sauvegarde stats annonces: {e}")


def enregistrer_annonce(user_id, nom):
    """Compte une annonce de plus pour ce chauffeur aujourd'hui (remis à zéro chaque jour)."""
    if not user_id:
        return
    data = charger_stats_jour()
    cle = str(user_id)
    info = data["compteurs"].get(cle, {"nom": nom, "nombre": 0})
    info["nom"] = nom or info.get("nom", "quelqu'un")
    info["nombre"] += 1
    data["compteurs"][cle] = info
    sauver_stats_jour(data)


def commande_top_annonces():
    data = charger_stats_jour()
    compteurs = data.get("compteurs", {})
    if not compteurs:
        return "Aucune annonce enregistrée aujourd'hui."
    classement = sorted(compteurs.values(), key=lambda x: x["nombre"], reverse=True)[:5]
    lignes = [f"{i + 1}. {c['nom']} — {c['nombre']} annonce(s)" for i, c in enumerate(classement)]
    return "🏆 <b>Top annonces du jour</b>\n\n" + "\n".join(lignes)


def envoyer_stats_soir_si_besoin():
    global dernier_stats_soir
    aujourdhui = maintenant().date()
    if maintenant().hour != HEURE_STATS_SOIR or dernier_stats_soir == aujourdhui:
        return
    envoyer_telegram(encadrer_message(commande_top_annonces()), silencieux=True)
    dernier_stats_soir = aujourdhui


def commande_aide():
    ligne = "─" * 20
    return (
        f"{ligne}\n"
        "📋 <b>AIDE &amp; COMMANDES</b>\n"
        "<i>Tout ce qu'il vous faut en un coup d'œil</i>\n"
        f"{ligne}\n\n"

        "✈️ <b>VOLS &amp; RECHERCHES</b>\n"
        "<code>/prochain</code>  Prochain vol attendu\n"
        "<code>/t1</code>  Terminal 1 (1h) · <code>/t1+</code> (3h)\n"
        "<code>/t2</code>  Terminal 2 (1h) · <code>/t2+</code> (3h)\n"
        "<code>/vol NUMERO</code>  Chercher un vol précis\n"
        "<code>/suivi NUMERO</code>  Être tagué à l'approche et à l'atterrissage d'un vol\n"
        "<code>/sncf</code>  Prochains trains Nice-Ville, TGV/TER (1h)\n"
        "<code>/circulation</code>  État trafic Monaco → Cannes (A8)\n"
        "<code>/annonce TEXTE</code>  Message prioritaire (admins uniquement)\n"
        "<code>/repos</code>  Couleur en repos aujourd'hui\n"
        "<code>/etat</code>  Voitures aux terminaux\n"
        "<code>/top</code>  🏆 Top 5 des annonces du jour\n"
        f"{ligne}\n\n"

        "🚖 <b>SIGNALER DES VOITURES</b>\n"
        "• Bouton <b>🚖 Signaler</b> en bas de l'écran (2 taps)\n"
        "• Ou tape <code>/v</code>\n"
        "• Ou écris directement :\n"
        "  <code>T2 linéaire 15v</code> · <code>T2 15 pk</code>\n"
        "  <code>T1 linéaire 4</code> · <code>T1 10 babel</code>\n"
        f"{ligne}\n\n"

        "🚨 <b>ALERTE VOLÉE</b> <i>(trop de monde)</i>\n"
        "Si pas assez de taxis pour la demande :\n"
        "• Tape <code>v1</code> ou <code>v2</code>\n"
        "• Ou boutons VOLÉE dans <code>/signaler</code>\n"
        f"{ligne}"
    )


def traiter_commandes(vols, trains=None):
    global dernier_update_id
    trains = trains or []
    updates = recuperer_updates_telegram()
    for u in updates:
        dernier_update_id = u["update_id"]

        callback = u.get("callback_query")
        if callback:
            traiter_callback(callback)
            continue

        message = u.get("message") or u.get("channel_post")
        if not message:
            continue
        texte = (message.get("text") or "").strip()
        chat_id = message["chat"]["id"]

        if not texte.startswith("/"):
            if texte in ("🚖 Signaler", "🚖", "SIGNALER"):
                supprimer_message_telegram(chat_id, message["message_id"])
                mid = envoyer_telegram_clavier(chat_id, "🚖 Choisis l'emplacement :", clavier_emplacements())
                if mid:
                    menus_en_attente[mid] = {"chat_id": chat_id, "envoye": maintenant()}
                continue

            if "GARE:" in texte and texte.startswith("T1"):
                # Tap accidentel sur le bouton d'état (son texte change à chaque fois,
                # donc pas de comparaison exacte possible) : on l'efface, rien à faire.
                supprimer_message_telegram(chat_id, message["message_id"])
                continue

            resultat_maj_pax = detecter_maj_pax_volee(texte)
            if resultat_maj_pax == "ambigu":
                repondre_telegram(chat_id, "Plusieurs volées actives en même temps — précise le terminal, ex: <code>t2 10</code>.")
                continue
            elif resultat_maj_pax:
                terminal_code, nb_pax = resultat_maj_pax
                qui = (message.get("from") or {}).get("first_name", "quelqu'un")
                supprimer_message_telegram(chat_id, message["message_id"])
                repondre_telegram(chat_id, texte_maj_pax_volee(terminal_code, nb_pax, qui), silencieux=False)
                continue

            resultat_volee_pax = detecter_volee_pax(texte)
            if resultat_volee_pax:
                terminal_code, nb_pax = resultat_volee_pax
                qui = (message.get("from") or {}).get("first_name", "quelqu'un")
                supprimer_message_telegram(chat_id, message["message_id"])
                if terminal_code not in volee_active:
                    nouveau_message_id = envoyer_alerte_volee(chat_id, terminal_code, qui, nb_pax)
                    if nouveau_message_id:
                        volee_active[terminal_code] = {"debut": maintenant(), "message_id": nouveau_message_id}
                continue

            if texte.lower() in ("v1", "v2"):
                terminal_code = texte.lower()
                label = "TERMINAL 1" if terminal_code == "v1" else "TERMINAL 2"
                supprimer_message_telegram(chat_id, message["message_id"])
                envoyer_telegram_clavier(
                    chat_id,
                    f"⚠️ Confirmer l'alerte VOLÉE {label} ? Tout le monde va être prévenu.",
                    clavier_confirmation_volee(terminal_code)
                )
                continue

            user_id = (message.get("from") or {}).get("id")
            cle_attente = (chat_id, user_id)

            if cle_attente in attente_pax_volee:
                m = re.search(r"\d+", texte)
                if m:
                    terminal_code, prompt_id, qui_declencheur = attente_pax_volee.pop(cle_attente)
                    nb_pax = int(m.group())
                    supprimer_message_telegram(chat_id, message["message_id"])
                    if prompt_id:
                        supprimer_message_telegram(chat_id, prompt_id)
                    if terminal_code not in volee_active:
                        nouveau_message_id = envoyer_alerte_volee(chat_id, terminal_code, qui_declencheur, nb_pax)
                        if nouveau_message_id:
                            volee_active[terminal_code] = {"debut": maintenant(), "message_id": nouveau_message_id}
                else:
                    repondre_telegram(chat_id, "Envoie juste un nombre, ex: 25")
                continue

            if cle_attente in attente_nombre_personnalise:
                m = re.search(r"\d+", texte)
                if m:
                    terminal, mode, prompt_id, type_action = attente_nombre_personnalise.pop(cle_attente)
                    nombre = int(m.group())
                    qui = (message.get("from") or {}).get("first_name", "quelqu'un")
                    definir_position(terminal, nombre, mode, qui)
                    enregistrer_annonce(user_id, qui)
                    supprimer_message_telegram(chat_id, message["message_id"])  # leur nombre tapé
                    if prompt_id:
                        supprimer_message_telegram(chat_id, prompt_id)  # la question posée
                    if type_action == "descente":
                        repondre_telegram(chat_id, texte_descente_babel(qui, nombre), silencieux=False)
                    else:
                        repondre_telegram(chat_id, f"🟧 ✅ {label_position(terminal, mode)} : <b>{nombre}</b> voitures\n<i>Signalé par {qui}</i>", silencieux=False)
                else:
                    repondre_telegram(chat_id, "Envoie juste un nombre, ex: 12")
                continue

            # 'Ça tire T1/T2' et signalement voitures : uniquement sur des messages courts,
            # pour éviter qu'un message long (annonce, discussion...) soit mal interprété
            # juste parce qu'il contient incidemment ces mots-clés.
            if len(texte) <= 40:
                terminal_tire = detecter_ca_tire(texte)
                if terminal_tire:
                    qui = (message.get("from") or {}).get("first_name", "quelqu'un")
                    definir_position(terminal_tire, "TIRE", None, qui)
                    label = "Terminal 1" if terminal_tire == "t1" else "Terminal 2"
                    supprimer_message_telegram(chat_id, message["message_id"])
                    repondre_telegram(
                        chat_id,
                        f"🟧 ⚡ <b>Rythme soutenu signalé à {label}</b>\n"
                        "Ça commence à tirer, restez prêts !\n"
                        f"<i>Signalé par {qui}</i>",
                        silencieux=False
                    )
                    continue

                # Message libre : on tente de le lire comme un signalement de voitures
                resultat = detecter_special_t2(texte) or detecter_full_parking_t2(texte) or detecter_places_restantes_t2(texte) or detecter_gare(texte) or detecter_format_court_lineaire(texte) or detecter_bb_babel(texte) or parser_position(texte)
                if resultat == "ambigu":
                    repondre_telegram(chat_id, "Précise le terminal : par exemple '3 linéaire t1' ou '3 linéaire t2'.")
                elif resultat:
                    terminal, nombre, mode = resultat
                    qui = (message.get("from") or {}).get("first_name", "quelqu'un")
                    user_id = (message.get("from") or {}).get("id")
                    definir_position(terminal, nombre, mode, qui)
                    enregistrer_annonce(user_id, qui)
                    supprimer_message_telegram(chat_id, message["message_id"])
                    if nombre == "A4":
                        valeur_txt = "<b>A4</b> (½ parking)"
                    elif nombre == "3/4":
                        valeur_txt = "<b>3/4</b> (linéaire presque plein)"
                    elif nombre == "FULL":
                        if mode == "parking":
                            valeur_txt = "<b>FULL</b> (parking T2 plein)"
                        elif mode == "reserve":
                            valeur_txt = "<b>FULL</b> (Babel plein)"
                        elif terminal == "gare":
                            valeur_txt = "<b>FULL</b> (Gare pleine)"
                        else:
                            valeur_txt = "<b>FULL</b> (fin linéaire T2)"
                    elif isinstance(nombre, str) and nombre.startswith("Q") and nombre[1:].isdigit():
                        valeur_txt = f"<b>Quasi full</b> — encore {nombre[1:]} place(s)"
                    else:
                        valeur_txt = f"<b>{nombre}</b> voitures"
                    repondre_telegram(chat_id, f"🟧 ✅ {label_position(terminal, mode)} : {valeur_txt}\n<i>Signalé par {qui}</i>", silencieux=False)
            continue

        partie = texte.split()
        commande = partie[0].lower().split("@")[0]  # gère /prochain@NomDuBot

        if commande in ("/prochain", "/next"):
            repondre_telegram(chat_id, commande_prochain(vols))
        elif commande == "/t1":
            repondre_telegram(chat_id, commande_terminal(vols, "1"))
        elif commande == "/t2":
            repondre_telegram(chat_id, commande_terminal(vols, "2"))
        elif commande == "/t1+":
            repondre_telegram(chat_id, commande_terminal(vols, "1", minutes=180))
        elif commande == "/t2+":
            repondre_telegram(chat_id, commande_terminal(vols, "2", minutes=180))
        elif commande in ("/vol", "/flight") and len(partie) > 1:
            repondre_telegram(chat_id, commande_vol(vols, partie[1]))
        elif commande == "/suivi" and len(partie) > 1:
            user_id = (message.get("from") or {}).get("id")
            nom = (message.get("from") or {}).get("first_name", "quelqu'un")
            repondre_telegram(chat_id, commande_suivi(partie[1], user_id, nom))
        elif commande == "/sncf":
            repondre_telegram(chat_id, commande_sncf(trains))
        elif commande == "/annonce":
            user_id = (message.get("from") or {}).get("id")
            nom = (message.get("from") or {}).get("first_name", "quelqu'un")
            if not est_admin_telegram(chat_id, user_id):
                repondre_telegram(chat_id, "🚫 Cette commande est réservée aux administrateurs du groupe.")
            else:
                contenu = texte[len(partie[0]):].strip()
                if not contenu:
                    repondre_telegram(chat_id, "Écris ton message après la commande, ex : <code>/annonce Tout le monde en Blanc demain à 6h.</code>")
                else:
                    supprimer_message_telegram(chat_id, message["message_id"])
                    envoyer_telegram(texte_annonce(contenu, nom), silencieux=False)
        elif commande == "/circulation":
            repondre_telegram(chat_id, commande_circulation(circulation_cache))
        elif commande == "/repos":
            repondre_telegram(chat_id, commande_repos())
        elif commande == "/etat":
            repondre_telegram(chat_id, commande_etat_file())
        elif commande == "/vide":
            repondre_telegram(chat_id, commande_vider_file())
        elif commande == "/top":
            repondre_telegram(chat_id, commande_top_annonces())
        elif commande in ("/signaler", "/rapide", "/v"):
            mid = envoyer_telegram_clavier(chat_id, "🚖 Choisis l'emplacement :", clavier_emplacements())
            if mid:
                menus_en_attente[mid] = {"chat_id": chat_id, "envoye": maintenant()}
        elif commande in ("/aide", "/help", "/start"):
            repondre_telegram(chat_id, commande_aide())
        else:
            repondre_telegram(chat_id, "Commande inconnue. Tape /aide pour la liste.")


# =========================
# RÉSUMÉ DU MATIN
# =========================

def en_pause_resume_fixe():
    """True entre 01:30 et 07:29 inclus : pas de gros résumé pendant cette fenêtre."""
    h, m = maintenant().hour, maintenant().minute
    debut_h, debut_m = PAUSE_RESUME_HEURE_DEBUT
    fin_h, fin_m = PAUSE_RESUME_HEURE_FIN
    minutes_actuelles = h * 60 + m
    minutes_debut = debut_h * 60 + debut_m
    minutes_fin = fin_h * 60 + fin_m
    return minutes_debut <= minutes_actuelles < minutes_fin


def slot_heure_actuel():
    """Créneau fixe actuel (jour, heure), pour aligner le résumé sur des horaires
    ronds (13h00, 14h00...) plutôt que sur un minuteur flottant."""
    now = maintenant()
    return (now.date(), now.hour)


def creer_resume_nuit(vols):
    """Résumé spécial 23h, pour les plus courageux : TOUS les vols restants de la nuit,
    retards inclus, jusqu'à 4h du matin — même ceux après minuit."""
    maintenant_dt = maintenant()
    limite = maintenant_dt.replace(hour=4, minute=0, second=0, microsecond=0)
    if limite <= maintenant_dt:
        limite += timedelta(days=1)

    restants = [
        v for v in vols
        if v.get("dt_actuel") and maintenant_dt <= v["dt_actuel"].astimezone(PARIS) <= limite
        and not est_arrive_ou_approche(v.get("site_status"))
    ]
    restants.sort(key=lambda v: v["dt_actuel"])

    if not restants:
        return "🌙 <b>Résumé de nuit</b>\nAucun vol restant annoncé jusqu'à 4h du matin."

    t1 = [v for v in restants if v["terminal"] == "1"]
    t2 = [v for v in restants if v["terminal"] == "2"]

    msg = f"🌙 <b>Résumé de nuit</b> — tous les vols restants jusqu'à 4h\n\n"
    msg += bloc_terminal("🔵 Terminal 1", t1).strip()
    msg += f"\n{SEPARATEUR_RESUME}\n"
    msg += bloc_terminal("🔴 Terminal 2", t2).strip()
    return msg


def boucle_principale():
    global dernier_resume, dernier_slot_resume, dernier_envoi_alertes_nuit
    init_db()
    reancrage_anti_spam_fait = False
    heure_demarrage_str = maintenant().strftime("%d/%m • %H:%M")
    train_ligne = (
        f"[ OK ] Train Engine         → TGV & TER • {SNCF_GARE_NOM}"
        if SNCF_API_TOKEN
        else "[ !! ] Train Engine         → DÉSACTIVÉ (SNCF_API_TOKEN manquant)"
    )
    message_demarrage = (
        "╔════════════════════════════════════╗\n"
        "║      EasyTaxi Flight Alert        ║\n"
        "║      DEPLOYMENT COMPLETED ✅      ║\n"
        "╚════════════════════════════════════╝\n\n"
        f"🕒 Release : {heure_demarrage_str}\n\n"
        "[ OK ] Flight Engine        → ONLINE\n"
        "[ OK ] Airport Data         → Official source &amp; Premium API synchronized\n"
        f"{train_ligne}\n"
        "[ OK ] Private Infrastructure → ONLINE\n"
        "[ OK ] Performance          → Optimizations applied\n"
        "[ OK ] Security             → Services secured\n\n"
        "💻 Developed &amp; Maintained by Tony\n\n"
        "🟢 SYSTEM STATUS : OPERATIONAL"
    )
    envoyer_telegram(message_demarrage, silencieux=True)

    # Fin de la période de grâce : pendant les 90s qui suivent un redémarrage,
    # aucune alerte ni résumé automatique n'est envoyé (seul le message ci-dessus part),
    # même si un créneau de résumé fixe (13h00, 13h30...) tombe pile à ce moment-là.
    fin_grace_demarrage = maintenant() + timedelta(seconds=90)

    try:
        vols = mettre_a_jour_cache_si_besoin(force=True)
        initialiser_sans_spam(vols)

        trains = mettre_a_jour_cache_trains_si_besoin(force=True)
        initialiser_sans_spam_trains(trains)

        mettre_a_jour_evenements_si_besoin(force=True)
        mettre_a_jour_matchs_concerts_si_besoin(force=True)

        mettre_a_jour_cache_circulation_si_besoin(force=True)

        dernier_resume = maintenant()
        dernier_slot_resume = slot_heure_actuel()
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        envoyer_telegram(f"⚠️ Erreur démarrage : {e}", silencieux=True)
        vols, trains = [], []

    while True:
        try:
            nettoyer_caches_si_besoin()
            verifier_expiration_volees()
            mettre_a_jour_evenements_si_besoin()
            mettre_a_jour_matchs_concerts_si_besoin()

            vols = mettre_a_jour_cache_si_besoin(force=False)
            trains = mettre_a_jour_cache_trains_si_besoin(force=False)
            circulation = mettre_a_jour_cache_circulation_si_besoin(force=False)

            en_periode_grace = maintenant() < fin_grace_demarrage

            # Juste à la sortie de la période de grâce, on re-mémorise silencieusement
            # les vols/trains déjà posés à partir des données les PLUS FRAÎCHES (pas celles
            # du tout premier scan au démarrage). Ça évite qu'un léger écart de texte entre
            # les deux scans (espace, accent...) ne fasse "oublier" un vol déjà vu et
            # déclenche un rappel en masse de tous les vols déjà posés depuis minuit.
            if not en_periode_grace and not reancrage_anti_spam_fait:
                initialiser_sans_spam(vols)
                initialiser_sans_spam_trains(trains)
                reancrage_anti_spam_fait = True

            # Petites alertes (approche/posé/retard/annulé) : envoi immédiat normalement,
            # mais entre 2h et 7h du matin on les regroupe et espace toutes les 15 min
            # pour ne pas multiplier les notifications nocturnes.
            if not en_periode_grace:
                heure_actuelle = maintenant().hour
                if NUIT_ESPACEMENT_HEURE_DEBUT <= heure_actuelle < NUIT_ESPACEMENT_HEURE_FIN:
                    if (dernier_envoi_alertes_nuit is None
                            or (maintenant() - dernier_envoi_alertes_nuit).total_seconds() >= NUIT_ESPACEMENT_SECONDES):
                        envoyer_alertes(vols)
                        envoyer_alertes_trains(trains)
                        dernier_envoi_alertes_nuit = maintenant()
                else:
                    envoyer_alertes(vols)
                    envoyer_alertes_trains(trains)

                envoyer_stats_soir_si_besoin()
                envoyer_alertes_circulation(circulation)

            # Gros résumé : sur des créneaux fixes (13h00, 13h30, 14h00...),
            # jamais entre 01:30 et 07:29 inclus, jamais pendant la période de grâce.
            slot_actuel = slot_heure_actuel()
            if not en_periode_grace and not en_pause_resume_fixe() and slot_actuel != dernier_slot_resume:
                d60 = vols_dans_minutes(vols, 60)
                trains_60 = trains_dans_minutes(trains, 60) if trains else []
                rien_a_signaler = len(d60) == 0 and len(trains_60) == 0
                if _en_heures_creuses() and rien_a_signaler:
                    logger.info("Résumé périodique sauté (heures creuses, rien à signaler).")
                else:
                    envoyer_telegram(creer_resume(vols, trains), silencieux=False)
                dernier_resume = maintenant()
                dernier_slot_resume = slot_actuel

        except Exception as e:
            logger.error(f"Erreur boucle: {e}")
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}", silencieux=True)

        time.sleep(10)


def boucle_commandes():
    """Thread dédié aux commandes/boutons Telegram, séparé de la boucle vols/trains,
    pour une réactivité quasi instantanée (long-polling, pas de délai de 10s)."""
    while True:
        try:
            traiter_commandes(vols_cache, trains_cache)
        except Exception as e:
            logger.error(f"Erreur boucle commandes: {e}")
            time.sleep(1)


def boucle_expiration_menus():
    """Vérifie l'expiration des menus 'Choisis l'emplacement' sur un minuteur indépendant.
    Le long-polling Telegram (boucle_commandes) peut rester bloqué jusqu'à 20s en attendant
    un message ; si la vérification tournait dans cette même boucle, elle serait retardée
    d'autant, et le menu resterait affiché bien plus longtemps que MENU_TIMEOUT_SECONDES."""
    while True:
        try:
            verifier_expiration_menus()
        except Exception as e:
            logger.error(f"Erreur boucle expiration menus: {e}")
        time.sleep(1)


def main():
    """Supervisor : si boucle_principale plante malgré tout, on redémarre au lieu de mourir."""
    threading.Thread(target=boucle_commandes, daemon=True).start()
    threading.Thread(target=boucle_expiration_menus, daemon=True).start()
    while True:
        try:
            boucle_principale()
        except Exception as e:
            logger.critical(f"Crash total du bot : {e}")
            try:
                envoyer_telegram(f"🚨 <b>Crash total</b> : {e}\nRedémarrage automatique dans 30s.", silencieux=True)
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
