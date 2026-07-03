import os
import re
import time
import json
import html
import logging
import logging.handlers
import sqlite3
import requests
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004321946575")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "c43e14c814msh221d76b3577077ap15a88ajsna897fda6a4ef")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "aerodatabox.p.rapidapi.com")
AEROPORT_IATA = "NCE"
PARIS = ZoneInfo("Europe/Paris")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN et TELEGRAM_CHAT_ID doivent être définis en variables d'environnement.")

URL_SITE_AEROPORT = "https://www.nice.aeroport.fr/en/flights/arrivals"

# Site officiel = gratuit, on peut vérifier souvent
FREQUENCE_SITE_SECONDES = 60

# API AeroDataBox = payante et quotée, on vérifie moins souvent et seulement les vols prioritaires
FREQUENCE_API_LISTE_SECONDES = int(os.getenv("FREQUENCE_API_LISTE_SECONDES", "1800")) # 30 min par défaut
MAX_LIVE_CALLS_PAR_CYCLE = int(os.getenv("MAX_LIVE_CALLS_PAR_CYCLE", "2"))
FENETRE_LIVE_MINUTES = int(os.getenv("FENETRE_LIVE_MINUTES", "35"))

# ---- Quota API mensuel (garde-fou dur, indépendant des fréquences ci-dessus) ----
QUOTA_API_MENSUEL = int(os.getenv("QUOTA_API_MENSUEL", "5800"))
QUOTA_MARGE_SECURITE = int(os.getenv("QUOTA_MARGE_SECURITE", "150")) # on s'arrête avant la vraie limite
QUOTA_FICHIER = os.getenv("QUOTA_FICHIER", "quota_api.json")
QUOTA_SEUIL_ALERTE = 0.9 # avertir Telegram quand 90% du quota est consommé
quota_alerte_envoyee = False

FREQUENCE_RESUME_SECONDES = 1800
RETARD_IMPORTANT_MINUTES = 20

# ---- SNCF / TGV (API officielle gratuite, quota très généreux : 150k/mois) ----
SNCF_API_TOKEN = os.getenv("SNCF_API_TOKEN")
SNCF_GARE_NOM = os.getenv("SNCF_GARE_NOM", "Nice Ville")
SNCF_STOP_AREA_ID = os.getenv("SNCF_STOP_AREA_ID") # optionnel : évite une résolution auto si déjà connu
SNCF_INCLURE_OUIGO = os.getenv("SNCF_INCLURE_OUIGO", "true").lower() == "true"
FREQUENCE_SNCF_SECONDES = int(os.getenv("FREQUENCE_SNCF_SECONDES", "180")) # 3 min, large quota donc pas besoin d'économiser
RETARD_TRAIN_IMPORTANT_MINUTES = int(os.getenv("RETARD_TRAIN_IMPORTANT_MINUTES", "15"))
APPROCHE_TRAIN_MINUTES = int(os.getenv("APPROCHE_TRAIN_MINUTES", "10"))

trains_cache = []
derniere_maj_trains = None
_stop_area_id_resolu = None
origine_cache = {} # vehicle_journey_id -> {"origine": str, "time": datetime}

trains_approche_annonces = set()
trains_arrives_annonces = set()
trains_annules_annonces = set()
trains_retard_annonces = {}


DB_FICHIER = os.getenv("DB_FICHIER", "historique_vols.db")

# ---- Commandes Telegram (polling gratuit, données cache uniquement) ----
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
dernier_update_id = None

# ---- Résumé du matin ----
RESUME_MATIN_HEURE = int(os.getenv("RESUME_MATIN_HEURE", "6"))
dernier_resume_matin = None

# ---- Watchdog scraping site ----
WATCHDOG_SEUIL_ECHECS = 3
echecs_site_consecutifs = 0
alerte_watchdog_envoyee = False

# ---- Nettoyage quotidien des caches ----
dernier_nettoyage = None

# ---- Annulations ----
annules_deja_annonces = set()

vols_cache = []
derniere_maj_site = None
derniere_maj_api = None
dernier_resume = None

live_status_cache = {}
LIVE_CACHE_MINUTES = 15

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
        return "🟣T2"
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


def sortie_passagers(v):
    if not v.get("dt_actuel"):
        return "🚖 Sortie clients estimée : bientôt"

    base_time = v["dt_actuel"].astimezone(PARIS)
    if str(v.get("terminal")) == "1":
        debut = base_time + timedelta(minutes=10)
        fin = base_time + timedelta(minutes=18)
    else:
        debut = base_time + timedelta(minutes=12)
        fin = base_time + timedelta(minutes=22)

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
            logger.error(f"Erreur Telegram: {r.text}")
    except Exception as e:
        logger.error(f"Erreur Telegram: {e}")


# =========================
# QUOTA API MENSUEL (garde-fou dur)
# =========================

def _charger_quota():
    try:
        with open(QUOTA_FICHIER, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    mois_actuel = maintenant().strftime("%Y-%m")
    if data.get("mois") != mois_actuel:
        data = {"mois": mois_actuel, "compteur": 0}
    return data


def _sauver_quota(data):
    try:
        with open(QUOTA_FICHIER, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Erreur sauvegarde quota: {e}")


def quota_restant():
    data = _charger_quota()
    return max(0, QUOTA_API_MENSUEL - data["compteur"])


def quota_utilise():
    return _charger_quota()["compteur"]


def api_disponible(cout=1):
    """Vérifie qu'on peut encore consommer `cout` appels ce mois-ci, marge de sécurité incluse."""
    return quota_restant() >= cout + QUOTA_MARGE_SECURITE


def consommer_quota(cout=1):
    global quota_alerte_envoyee
    data = _charger_quota()
    data["compteur"] += cout
    _sauver_quota(data)

    ratio = data["compteur"] / QUOTA_API_MENSUEL
    if ratio >= QUOTA_SEUIL_ALERTE and not quota_alerte_envoyee:
        envoyer_telegram(
            "⚠️ <b>Quota API bientôt atteint</b>\n"
            f"{data['compteur']}/{QUOTA_API_MENSUEL} appels utilisés ce mois-ci.\n"
            "Le bot va basculer sur le site aéroport uniquement jusqu'au mois prochain."
        )
        quota_alerte_envoyee = True


def jours_restants_mois():
    now = maintenant()
    if now.month == 12:
        prochain = now.replace(year=now.year + 1, month=1, day=1)
    else:
        prochain = now.replace(month=now.month + 1, day=1)
    return max(1, (prochain.date() - now.date()).days)


def budget_journalier_calls():
    """Nombre d'appels API qu'on peut encore se permettre par jour jusqu'à la fin du mois."""
    return max(0, quota_restant() // jours_restants_mois())




def recuperer_site_aeroport():
    headers = {"User-Agent": "Mozilla/5.0 EasyTaxiFlightAlert/12.0"}
    r = requests.get(URL_SITE_AEROPORT, headers=headers, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    lignes = [nettoyer(x) for x in soup.get_text("\n").split("\n")]
    return [x for x in lignes if x]


def est_heure(x):
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", x or ""))


def est_numero_vol(x):
    return bool(re.fullmatch(r"[A-Z0-9]{2,3}\s?\d{2,5}[A-Z]?", (x or "").strip()))


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


def ligne_ressemble_ville(x):
    if not x or est_heure(x) or est_numero_vol(x) or normaliser_terminal(x):
        return False
    bad = ["expected", "arrived", "landing", "delayed", "cancelled", "terminal", "flight", "arrival"]
    return not any(b in x.lower() for b in bad) and len(x) >= 3


def decouper_blocs_site(lignes):
    debuts = []
    for i in range(len(lignes) - 1):
        if est_heure(lignes[i]) and ligne_ressemble_ville(lignes[i + 1]):
            debuts.append(i)
    blocs = []
    for idx, debut in enumerate(debuts):
        fin = debuts[idx + 1] if idx + 1 < len(debuts) else min(debut + 40, len(lignes))
        blocs.append(lignes[debut:fin])
    return blocs


def recuperer_vols_site():
    lignes = recuperer_site_aeroport()
    blocs = decouper_blocs_site(lignes)
    vols = []

    mots_statut = ["Arrived", "Expected", "Delayed", "Landing", "Cancelled", "Prévu", "Approche", "Retard", "Atterri"]

    for bloc in blocs:
        if len(bloc) < 3:
            continue

        heure = bloc[0]
        ville = nettoyer_nom(bloc[1])
        terminal = None
        numero = "N/A"
        compagnie = "N/A"
        status = "Expected"

        for x in bloc:
            t = normaliser_terminal(x)
            if t:
                terminal = t
            if est_numero_vol(x):
                numero = x.replace(" ", "")
            if any(m.lower() in x.lower() for m in mots_statut):
                status = statut_site_lisible(x)

        for x in bloc[2:]:
            if x in [heure, ville, terminal, numero]:
                continue
            if normaliser_terminal(x) or est_heure(x) or est_numero_vol(x):
                continue
            if any(m.lower() in x.lower() for m in mots_statut):
                continue
            if len(x) >= 3:
                compagnie = nettoyer_compagnie(x)
                break

        if terminal in ["1", "2"]:
            heure_status = extraire_heure(status)
            actuel = heure_status or heure
            vols.append({
                "numero": numero,
                "compagnie": compagnie,
                "provenance": ville,
                "terminal": terminal,
                "status": "Expected",
                "live_status": None,
                "site_status": status,
                "dt_prevu": heure_aujourdhui(heure),
                "dt_actuel": heure_aujourdhui(actuel),
                "prevu": heure,
                "actuel": actuel,
                "retard": minutes_retard(heure, actuel),
                "source": "site"
            })

    return dedoublonner_vols(vols)


def heure_aujourdhui(hh):
    try:
        h, m = map(int, hh.split(":"))
        return maintenant().replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return None


def minutes_retard(prevu, actuel):
    try:
        a = heure_aujourdhui(prevu)
        b = heure_aujourdhui(actuel)
        if not a or not b:
            return 0
        return max(0, int((b - a).total_seconds() // 60))
    except Exception:
        return 0


# =========================
# AERODATABOX
# =========================

def recuperer_arrivees_aerodatabox():
    if not RAPIDAPI_KEY:
        raise Exception("Clé RapidAPI absente")
    if not api_disponible(1):
        raise Exception(f"Quota API mensuel atteint ({quota_utilise()}/{QUOTA_API_MENSUEL}), passage en mode site uniquement")

    debut = maintenant() - timedelta(minutes=15)
    fin = maintenant() + timedelta(hours=2)
    url = f"https://{RAPIDAPI_HOST}/flights/airports/iata/{AEROPORT_IATA}/{debut.strftime('%Y-%m-%dT%H:%M')}/{fin.strftime('%Y-%m-%dT%H:%M')}"

    params = {
        "withLeg": "true",
        "direction": "Arrival",
        "withCancelled": "true",
        "withCodeshared": "true",
        "withCargo": "false",
        "withPrivate": "false",
    }
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}

    r = requests.get(url, headers=headers, params=params, timeout=25)
    consommer_quota(1)
    if not r.ok:
        raise Exception(f"AeroDataBox {r.status_code}: {r.text[:250]}")

    vols = []
    for item in r.json().get("arrivals", []):
        arrival = item.get("arrival", {}) or {}
        departure = item.get("departure", {}) or {}
        airline = item.get("airline", {}) or {}
        dep_airport = departure.get("airport", {}) or {}

        scheduled = arrival.get("scheduledTime", {}) or {}
        revised = arrival.get("revisedTime", {}) or {}
        actual = arrival.get("actualTime", {}) or {}
        predicted = arrival.get("predictedTime", {}) or {}

        dt_prevu = parse_iso(scheduled.get("local"))
        dt_actuel = parse_iso(actual.get("local")) or parse_iso(revised.get("local")) or parse_iso(predicted.get("local")) or dt_prevu

        retard = 0
        if dt_prevu and dt_actuel:
            retard = max(0, int((dt_actuel - dt_prevu).total_seconds() // 60))

        vols.append({
            "numero": item.get("number") or item.get("callSign") or "N/A",
            "compagnie": nettoyer_compagnie(airline.get("name") or "N/A"),
            "provenance": nettoyer_nom(dep_airport.get("municipalityName") or dep_airport.get("name") or dep_airport.get("iata") or "INCONNUE"),
            "terminal": str(arrival.get("terminal") or ""),
            "status": item.get("status") or arrival.get("status") or "Expected",
            "live_status": None,
            "site_status": None,
            "dt_prevu": dt_prevu,
            "dt_actuel": dt_actuel,
            "prevu": hhmm(dt_prevu),
            "actuel": hhmm(dt_actuel),
            "retard": retard,
            "source": "api"
        })

    return dedoublonner_vols(vols)


def dedoublonner_vols(vols):
    uniques = {}
    for v in vols:
        cle = f"{v.get('actuel')}-{v.get('provenance')}-{v.get('terminal')}"
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


def fusionner_site_api(vols_site, vols_api):
    """
    Le site est gratuit et peut donner Approche/Arrived.
    L'API donne mieux les noms/numéros/terminaux.
    On fusionne par terminal + ville + heure proche.
    """
    resultats = []
    utilises_site = set()

    for api in vols_api:
        meilleur = None
        meilleur_idx = None
        for i, site in enumerate(vols_site):
            if i in utilises_site:
                continue
            if api["terminal"] != site["terminal"]:
                continue
            if api["provenance"] != site["provenance"]:
                continue
            if api["dt_actuel"] and site["dt_actuel"]:
                diff = abs((api["dt_actuel"] - site["dt_actuel"]).total_seconds()) / 60
                if diff <= 20:
                    meilleur = site
                    meilleur_idx = i
                    break

        if meilleur:
            api["site_status"] = meilleur.get("site_status")
            if meilleur.get("retard", 0) > api.get("retard", 0):
                api["retard"] = meilleur["retard"]
                api["actuel"] = meilleur["actuel"]
                api["dt_actuel"] = meilleur["dt_actuel"]
            utilises_site.add(meilleur_idx)

        resultats.append(api)

    for i, site in enumerate(vols_site):
        if i not in utilises_site:
            resultats.append(site)

    return dedoublonner_vols(resultats)


def _verifier_watchdog_site(vols_site):
    """Alerte si le scraping du site retourne 0 vol plusieurs fois de suite
    (signe probable que la structure du site a changé)."""
    global echecs_site_consecutifs, alerte_watchdog_envoyee
    if len(vols_site) == 0:
        echecs_site_consecutifs += 1
        logger.warning(f"Scraping site: 0 vol trouvé ({echecs_site_consecutifs}/{WATCHDOG_SEUIL_ECHECS})")
        if echecs_site_consecutifs >= WATCHDOG_SEUIL_ECHECS and not alerte_watchdog_envoyee:
            envoyer_telegram(
                "🚨 <b>Alerte scraping</b>\n"
                "Le site aéroport ne retourne plus aucun vol depuis plusieurs vérifications.\n"
                "La structure de la page a peut-être changé — vérification manuelle recommandée."
            )
            alerte_watchdog_envoyee = True
    else:
        echecs_site_consecutifs = 0
        alerte_watchdog_envoyee = False


def mettre_a_jour_cache_si_besoin(force=False):
    global vols_cache, derniere_maj_site, derniere_maj_api

    vols_site = None
    if force or derniere_maj_site is None or (maintenant() - derniere_maj_site).total_seconds() >= FREQUENCE_SITE_SECONDES:
        try:
            vols_site = recuperer_vols_site()
            derniere_maj_site = maintenant()
            _verifier_watchdog_site(vols_site)
        except Exception as e:
            logger.warning(f"Erreur site aéroport: {e}")

    vols_api = None
    if force or derniere_maj_api is None or (maintenant() - derniere_maj_api).total_seconds() >= FREQUENCE_API_LISTE_SECONDES:
        try:
            vols_api = recuperer_arrivees_aerodatabox()
            derniere_maj_api = maintenant()
        except Exception as e:
            logger.warning(f"Erreur API liste: {e}")

    if vols_site is None and vols_api is None:
        return vols_cache

    if vols_site is None:
        vols_site = [v for v in vols_cache if v.get("source") == "site"]
    if vols_api is None:
        vols_api = [v for v in vols_cache if v.get("source") == "api"]

    if vols_api:
        vols_cache = fusionner_site_api(vols_site or [], vols_api)
    else:
        vols_cache = vols_site or vols_cache

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
    _stop_area_id_resolu = SNCF_STOP_AREA_ID or "stop_area:SNCF:87756056" # Nice-Ville, code UIC 87756056
    return _stop_area_id_resolu


def est_tgv(commercial_mode):
    cm = (commercial_mode or "").upper()
    if "OUIGO" in cm:
        return SNCF_INCLURE_OUIGO
    return "TGV" in cm


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
    params = {"count": 30, "duration": 7200} # fenêtre de 2h
    r = requests.get(url, params=params, auth=(SNCF_API_TOKEN, ""), timeout=20)
    r.raise_for_status()
    data = r.json()

    trains = []
    for item in data.get("arrivals", []):
        infos = item.get("display_informations", {}) or {}
        stop_dt = item.get("stop_date_time", {}) or {}

        commercial_mode = infos.get("commercial_mode", "")
        if not est_tgv(commercial_mode):
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


def mettre_a_jour_cache_trains_si_besoin(force=False):
    global trains_cache, derniere_maj_trains

    if not SNCF_API_TOKEN:
        return trains_cache # module désactivé tant qu'aucun token n'est fourni

    if not force and derniere_maj_trains is not None and (maintenant() - derniere_maj_trains).total_seconds() < FREQUENCE_SNCF_SECONDES:
        return trains_cache

    try:
        trains_cache = recuperer_arrivees_sncf()
        derniere_maj_trains = maintenant()
    except Exception as e:
        logger.warning(f"Erreur API SNCF: {e}")

    return trains_cache


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


def ligne_train(t):
    heure = heure_lisible_train(t)
    provenance = html.escape((t.get("provenance") or "")[:15])
    voie = html.escape((t.get("voie") or "")[:6])
    return f"{heure:<12} {provenance:<16} {voie:<7} {icone_train(t)}"


def trains_dans_minutes(trains, minutes):
    now = maintenant()
    limite = now + timedelta(minutes=minutes)
    return [t for t in trains if t.get("dt_actuel") and now <= t["dt_actuel"] <= limite]


def envoyer_alertes_trains(trains):
    for t in trains:
        cle = cle_train(t)
        statut = statut_train(t)

        if statut == "annule" and cle not in trains_annules_annonces:
            envoyer_telegram(
                "❌ <b>TGV ANNULÉ</b>\n\n"
                f"🚄 Train {t['numero']}\n"
                f"🕒 Prévu {t['prevu']}"
            )
            trains_annules_annonces.add(cle)
            continue

        if statut == "approche" and cle not in trains_approche_annonces:
            envoyer_telegram(
                "🚄 <b>TGV EN APPROCHE</b>\n\n"
                f"Train {t['numero']} ({t.get('type', 'TGV')})\n"
                f"🕒 {heure_lisible_train(t)}"
                + (f"\n📍 Voie {t['voie']}" if t.get("voie") else "")
            )
            trains_approche_annonces.add(cle)

        if statut == "arrive" and cle not in trains_arrives_annonces:
            envoyer_telegram(
                "✅ <b>TGV ARRIVÉ</b>\n\n"
                f"Train {t['numero']} ({t.get('type', 'TGV')})\n"
                f"🕒 {t['actuel']}"
                + (f"\n📍 Voie {t['voie']}" if t.get("voie") else "")
            )
            trains_arrives_annonces.add(cle)

        if t.get("retard", 0) >= RETARD_TRAIN_IMPORTANT_MINUTES and statut not in ("arrive", "annule"):
            ancien = trains_retard_annonces.get(cle)
            if ancien is None or t["retard"] >= ancien + 10:
                envoyer_telegram(
                    "⏰ <b>RETARD TGV</b>\n\n"
                    f"Train {t['numero']}\n"
                    f"🕒 {heure_lisible_train(t)} (<b>+{t['retard']}min</b>)"
                )
                trains_retard_annonces[cle] = t["retard"]


# =========================
# LIVE STATUS API PRIORITAIRE
# =========================

def score_gros_vol(v):
    score = 0
    ville = (v.get("provenance") or "").upper()
    compagnie = (v.get("compagnie") or "").upper()
    gros_hubs = ["PARIS", "LONDRES", "FRANCFORT", "AMSTERDAM", "GENÈVE", "ZURICH", "CASABLANCA", "ISTANBUL", "DUBAI", "DOHA", "COPENHAGUE"]
    grosses_compagnies = ["AIR FRANCE", "BRITISH", "LUFTHANSA", "KLM", "SWISS", "EMIRATES", "QATAR", "TURKISH", "EASYJET", "TRANSAVIA", "SAS", "NORWEGIAN", "RAM"]
    if any(h in ville for h in gros_hubs):
        score += 50
    if any(c in compagnie for c in grosses_compagnies):
        score += 25
    if str(v.get("terminal")) == "2":
        score += 10
    dt = v.get("dt_actuel") or v.get("dt_prevu")
    if dt:
        mins = (dt.astimezone(PARIS) - maintenant()).total_seconds() / 60
        if 0 <= mins <= 10: score += 40
        elif mins <= 20: score += 30
        elif mins <= 35: score += 15
    return score


def recuperer_live_status(numero):
    if not numero or numero == "N/A":
        return None
    cached = live_status_cache.get(numero)
    if cached and (maintenant() - cached["time"]).total_seconds() < 15 * 60:
        return cached["status"]

    if not api_disponible(1):
        return None

    url = f"https://{RAPIDAPI_HOST}/flights/number/{numero}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        consommer_quota(1)
        if not r.ok:
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        choisi = None
        for item in data:
            airport = ((item.get("arrival") or {}).get("airport") or {})
            if airport.get("iata") == AEROPORT_IATA:
                choisi = item
                break
        choisi = choisi or data[0]
        status = choisi.get("status")
        live_status_cache[numero] = {"status": status, "time": maintenant()}
        return status
    except Exception:
        return None


def vols_dans_minutes(vols, minutes):
    now = maintenant()
    limite = now + timedelta(minutes=minutes)
    return [v for v in vols if v.get("dt_actuel") and now <= v["dt_actuel"].astimezone(PARIS) <= limite]


def enrichir_live_status(vols):
    candidats = vols_dans_minutes(vols, FENETRE_LIVE_MINUTES)
    candidats = [v for v in candidats if not est_arrive_ou_approche(v.get("site_status"))]
    candidats.sort(key=lambda v: (-score_gros_vol(v), v.get("dt_actuel") or v.get("dt_prevu") or maintenant()))

    limite_cycle = min(MAX_LIVE_CALLS_PAR_CYCLE, budget_journalier_calls())

    appels = 0
    for v in candidats:
        if appels >= limite_cycle:
            break
        if not api_disponible(1):
            break
        status = recuperer_live_status(v["numero"])
        appels += 1
        if status:
            v["live_status"] = status
    return vols


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

    trains_approche_annonces.clear()
    trains_arrives_annonces.clear()
    trains_annules_annonces.clear()
    trains_retard_annonces.clear()

    expiration_origine = maintenant() - timedelta(hours=6)
    obsoletes_origine = [k for k, v in origine_cache.items() if v["time"] < expiration_origine]
    for k in obsoletes_origine:
        del origine_cache[k]

    expiration = maintenant() - timedelta(hours=6)
    obsoletes = [k for k, v in live_status_cache.items() if v["time"] < expiration]
    for k in obsoletes:
        del live_status_cache[k]

    dernier_nettoyage = aujourdhui
    logger.info("Nettoyage quotidien des caches effectué.")


def niveau_affluence(nb30):
    if nb30 >= 8: return "🔴 Forte"
    if nb30 >= 4: return "🟠 Moyenne"
    return "🟢 Calme"


def icone_statut_court(v):
    status = v.get("site_status") or v.get("live_status") or v.get("status") or ""
    retard = v.get("retard", 0)

    if est_pose(status):
        return "✅"
    if est_approche(status):
        return "🛬"
    if est_en_route(status):
        return "✈️"
    if "cancel" in status.lower() or "annul" in status.lower():
        return "❌"
    if retard >= RETARD_IMPORTANT_MINUTES:
        return f"⏰+{retard}"
    if retard >= 10:
        return f"🟡+{retard}"
    return "🟢"


def ligne_vol(v):
    heure = heure_lisible(v)
    ville = html.escape((v['provenance'] or "")[:13])
    compagnie = html.escape((v['compagnie'] or "")[:11])
    return f"{heure:<12} {ville:<13} {compagnie:<11} {icone_statut_court(v)}"


def bloc_terminal(titre, vols):
    if not vols:
        return f"{titre} : 0\n"
    lignes = [f"{titre} : {len(vols)}"]
    corps = "\n".join(ligne_vol(v) for v in vols)
    return f"{lignes[0]}\n<code>{corps}</code>\n"


def bloc_trains(trains):
    if not trains:
        return ""
    corps = "\n".join(ligne_train(t) for t in trains[:8])
    return f"\n🚄 <b>Prochains TGV (30min)</b> : {len(trains)}\n<code>{corps}</code>\n"


def creer_resume(vols, trains=None):
    d30 = vols_dans_minutes(vols, 30)
    d60 = vols_dans_minutes(vols, 60)
    t1_30 = [v for v in d30 if v["terminal"] == "1"]
    t2_30 = [v for v in d30 if v["terminal"] == "2"]
    t1_60 = [v for v in d60 if v["terminal"] == "1"]
    t2_60 = [v for v in d60 if v["terminal"] == "2"]
    approches = [v for v in d30 if est_approche(v.get("site_status") or v.get("live_status") or v.get("status"))]
    poses = [v for v in d30 if est_pose(v.get("site_status") or v.get("live_status") or v.get("status"))]
    retards = [v for v in d60 if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(v.get("site_status") or v.get("live_status") or v.get("status"))]

    msg = (
        "✈️ <b>EASYTAXI FLIGHT ALERT</b>\n"
        f"🕒 {maintenant().strftime('%H:%M')} | 🚖 {niveau_affluence(len(d30))} | ⚠️ {len(retards)}\n"
        f"🛬 Approche : {len(approches)} | ✅ Posés : {len(poses)}\n"
        f"⏱️ 30min : <b>{len(d30)}</b> (🔵{len(t1_30)} / 🟣{len(t2_30)})\n"
        f"🕐 1h : <b>{len(d60)}</b> (🔵{len(t1_60)} / 🟣{len(t2_60)})\n\n"
        "🛬 <b>Prochains 30min</b>\n"
        "<i>🟢 prévu · 🟡/⏰ retard · 🛬 approche · ✅ posé</i>\n"
    )
    msg += bloc_terminal("🔵 T1", t1_30)
    msg += bloc_terminal("🟣 T2", t2_30)

    if trains:
        trains_30 = trains_dans_minutes(trains, 30)
        msg += bloc_trains(trains_30)

    msg += f"\n🔌 API vols : {quota_utilise()}/{QUOTA_API_MENSUEL} ce mois-ci"
    return msg.strip()


def initialiser_sans_spam(vols):
    for v in vols:
        cle = cle_vol(v)
        status = v.get("site_status") or v.get("live_status") or v.get("status")
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


def envoyer_alertes(vols):
    for v in vols:
        cle = cle_vol(v)
        status = v.get("site_status") or v.get("live_status") or v.get("status")

        if est_annule(status) and cle not in annules_deja_annonces:
            envoyer_telegram(
                "❌ <b>VOL ANNULÉ</b>\n\n"
                f"🌍 <b>{v['provenance']}</b>\n"
                f"✈️ {v['compagnie']}\n"
                f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
                f"🕒 Prévu {v['prevu']}"
            )
            annules_deja_annonces.add(cle)
            continue

        if est_approche(status) and cle not in approches_deja_annoncees:
            envoyer_telegram(
                "🛬 <b>EN APPROCHE</b>\n\n"
                f"🌍 <b>{v['provenance']}</b>\n"
                f"✈️ {v['compagnie']}\n"
                f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
                f"🕒 {heure_lisible(v)}"
            )
            approches_deja_annoncees.add(cle)

        if est_pose(status) and cle not in poses_deja_annonces:
            envoyer_telegram(
                "✅ <b>POSÉ</b>\n\n"
                f"🌍 <b>{v['provenance']}</b>\n"
                f"✈️ {v['compagnie']}\n"
                f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
                f"🕒 {v['actuel']}\n"
                f"{sortie_passagers(v)}"
            )
            poses_deja_annonces.add(cle)
            enregistrer_vol_historique(v)

        if v["retard"] >= RETARD_IMPORTANT_MINUTES and not est_arrive_ou_approche(status):
            ancien = retards_deja_annonces.get(cle)
            if ancien is None or v["retard"] >= ancien + 10:
                envoyer_telegram(
                    "⏰ <b>RETARD</b>\n\n"
                    f"🌍 <b>{v['provenance']}</b>\n"
                    f"✈️ {v['compagnie']}\n"
                    f"📍 {emoji_terminal(v['terminal'])} - {label_terminal(v['terminal'])}\n"
                    f"🕒 {heure_lisible(v)} (<b>+{v['retard']}min</b>)"
                )
                retards_deja_annonces[cle] = v["retard"]


# =========================
# COMMANDES TELEGRAM (getUpdates = API Telegram, gratuite et illimitée)
# Ces commandes ne lisent QUE vols_cache : elles ne déclenchent JAMAIS
# d'appel RapidAPI, donc aucun impact sur le quota mensuel.
# =========================

def recuperer_updates_telegram():
    global dernier_update_id
    params = {"timeout": 0}
    if dernier_update_id is not None:
        params["offset"] = dernier_update_id + 1
    try:
        r = requests.get(f"{TELEGRAM_API_URL}/getUpdates", params=params, timeout=15)
        if not r.ok:
            return []
        return r.json().get("result", [])
    except Exception as e:
        logger.error(f"Erreur getUpdates Telegram: {e}")
        return []


def repondre_telegram(chat_id, message):
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Erreur réponse commande Telegram: {e}")


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


def commande_terminal(vols, terminal):
    filtres = [v for v in vols_dans_minutes(vols, 60) if v["terminal"] == terminal]
    if not filtres:
        return f"Aucun vol prévu en Terminal {terminal} dans l'heure (données cache)."
    corps = "\n".join(ligne_vol(v) for v in filtres[:10])
    return f"📍 <b>Terminal {terminal}</b> (1h)\n<code>{corps}</code>"


def commande_vol(vols, numero):
    numero = (numero or "").upper().replace(" ", "")
    for v in vols:
        if (v.get("numero") or "").upper().replace(" ", "") == numero:
            return (
                f"✈️ <b>{v['numero']}</b> · {v['compagnie']}\n"
                f"De {v['provenance']} · {emoji_terminal(v['terminal'])}\n"
                f"{heure_lisible(v)} · {statut_lisible(v)}"
            )
    return (
        f"Vol {numero} introuvable dans les données actuelles.\n"
        "<i>Pas de recherche API déclenchée pour préserver le quota.</i>"
    )


def commande_quota():
    return f"🔌 Quota API : {quota_utilise()}/{QUOTA_API_MENSUEL} utilisés, {quota_restant()} restants ce mois-ci."


def commande_tgv(trains):
    if not SNCF_API_TOKEN:
        return "🚄 Module TGV pas encore activé (token SNCF manquant)."
    filtres = trains_dans_minutes(trains, 60)
    if not filtres:
        return "Aucun TGV prévu à Nice-Ville dans l'heure (données cache)."
    corps = "\n".join(ligne_train(t) for t in filtres[:10])
    return f"🚄 <b>TGV Nice-Ville</b> (1h)\n<code>{corps}</code>"


def commande_aide():
    return (
        "📋 <b>Commandes disponibles</b>\n"
        "/prochain — prochain vol attendu\n"
        "/t1 — vols Terminal 1 (1h)\n"
        "/t2 — vols Terminal 2 (1h)\n"
        "/vol NUMERO — chercher un vol précis\n"
        "/tgv — prochains TGV à Nice-Ville (1h)\n"
        "/quota — quota API restant\n\n"
        "<i>Toutes ces commandes utilisent uniquement les données déjà en cache "
        "(sources gratuites) — aucun appel API supplémentaire n'est déclenché.</i>"
    )


def traiter_commandes(vols, trains=None):
    global dernier_update_id
    trains = trains or []
    updates = recuperer_updates_telegram()
    for u in updates:
        dernier_update_id = u["update_id"]
        message = u.get("message") or u.get("channel_post")
        if not message:
            continue
        texte = (message.get("text") or "").strip()
        if not texte.startswith("/"):
            continue
        chat_id = message["chat"]["id"]
        partie = texte.split()
        commande = partie[0].lower().split("@")[0] # gère /prochain@NomDuBot

        if commande in ("/prochain", "/next"):
            repondre_telegram(chat_id, commande_prochain(vols))
        elif commande == "/t1":
            repondre_telegram(chat_id, commande_terminal(vols, "1"))
        elif commande == "/t2":
            repondre_telegram(chat_id, commande_terminal(vols, "2"))
        elif commande in ("/vol", "/flight") and len(partie) > 1:
            repondre_telegram(chat_id, commande_vol(vols, partie[1]))
        elif commande == "/tgv":
            repondre_telegram(chat_id, commande_tgv(trains))
        elif commande == "/quota":
            repondre_telegram(chat_id, commande_quota())
        elif commande in ("/aide", "/help", "/start"):
            repondre_telegram(chat_id, commande_aide())
        else:
            repondre_telegram(chat_id, "Commande inconnue. Tape /aide pour la liste.")


# =========================
# RÉSUMÉ DU MATIN
# =========================

def envoyer_resume_matin_si_besoin(vols):
    global dernier_resume_matin
    aujourdhui = maintenant().date()
    if maintenant().hour != RESUME_MATIN_HEURE or dernier_resume_matin == aujourdhui:
        return
    d60 = vols_dans_minutes(vols, 60)
    envoyer_telegram(
        "☀️ <b>Bonjour !</b>\n"
        f"{len(d60)} vols attendus dans l'heure qui vient.\n"
        "Bonne journée, le suivi reprend normalement 🚖"
    )
    dernier_resume_matin = aujourdhui




def boucle_principale():
    global dernier_resume
    init_db()
    message_demarrage = (
        "✅ <b>EasyTaxi Flight Alert V15 lancé</b>\n"
        "Site aéroport gratuit + API AeroDataBox (quota géré) + commandes /aide.\n"
        f"🔌 Quota API vols : {quota_utilise()}/{QUOTA_API_MENSUEL} utilisés ce mois-ci "
        f"({quota_restant()} restants, ~{budget_journalier_calls()}/jour)."
    )
    if SNCF_API_TOKEN:
        message_demarrage += f"\n🚄 Module TGV activé (gare : {SNCF_GARE_NOM})."
    else:
        message_demarrage += "\n🚄 Module TGV désactivé (SNCF_API_TOKEN manquant)."
    envoyer_telegram(message_demarrage)

    try:
        vols = mettre_a_jour_cache_si_besoin(force=True)
        vols = enrichir_live_status(vols)
        initialiser_sans_spam(vols)

        trains = mettre_a_jour_cache_trains_si_besoin(force=True)
        initialiser_sans_spam_trains(trains)

        envoyer_telegram(creer_resume(vols, trains))
        dernier_resume = maintenant()
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        envoyer_telegram(f"⚠️ Erreur démarrage : {e}")
        vols, trains = [], []

    while True:
        try:
            nettoyer_caches_si_besoin()

            vols = mettre_a_jour_cache_si_besoin(force=False)
            vols = enrichir_live_status(vols)
            envoyer_alertes(vols)

            trains = mettre_a_jour_cache_trains_si_besoin(force=False)
            envoyer_alertes_trains(trains)

            # Commandes Telegram : API Telegram uniquement, jamais RapidAPI ni SNCF
            traiter_commandes(vols, trains)

            envoyer_resume_matin_si_besoin(vols)

            if dernier_resume is None or (maintenant() - dernier_resume).total_seconds() >= FREQUENCE_RESUME_SECONDES:
                envoyer_telegram(creer_resume(vols, trains))
                dernier_resume = maintenant()

        except Exception as e:
            logger.error(f"Erreur boucle: {e}")
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}")

        time.sleep(10)


def main():
    """Supervisor : si boucle_principale plante malgré tout, on redémarre au lieu de mourir."""
    while True:
        try:
            boucle_principale()
        except Exception as e:
            logger.critical(f"Crash total du bot : {e}")
            try:
                envoyer_telegram(f"🚨 <b>Crash total</b> : {e}\nRedémarrage automatique dans 30s.")
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
