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

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "c43e14c814msh221d76b3577077ap15a88ajsna897fda6a4ef")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "aerodatabox.p.rapidapi.com")
AEROPORT_IATA = "NCE"
PARIS = ZoneInfo("Europe/Paris")

URL_SITE_AEROPORT = "https://www.nice.aeroport.fr/en/flights/arrivals"

# Site officiel = gratuit, on peut vérifier souvent
FREQUENCE_SITE_SECONDES = 60

# API AeroDataBox = payante, on vérifie rarement et seulement les vols prioritaires
FREQUENCE_API_LISTE_SECONDES = 900
MAX_LIVE_CALLS_PAR_CYCLE = int(os.getenv("MAX_LIVE_CALLS_PAR_CYCLE", "2"))
FENETRE_LIVE_MINUTES = int(os.getenv("FENETRE_LIVE_MINUTES", "35"))

FREQUENCE_RESUME_SECONDES = 1800
RETARD_IMPORTANT_MINUTES = 20

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
            print("Erreur Telegram:", r.text)
    except Exception as e:
        print("Erreur Telegram:", e)


# =========================
# SITE AÉROPORT GRATUIT
# =========================

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
    if not RAPIDAPI_KEY or RAPIDAPI_KEY == "COLLE_TA_CLE_RAPIDAPI_ICI":
        raise Exception("Clé RapidAPI absente")

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


def mettre_a_jour_cache_si_besoin(force=False):
    global vols_cache, derniere_maj_site, derniere_maj_api

    vols_site = None
    if force or derniere_maj_site is None or (maintenant() - derniere_maj_site).total_seconds() >= FREQUENCE_SITE_SECONDES:
        try:
            vols_site = recuperer_vols_site()
            derniere_maj_site = maintenant()
        except Exception as e:
            print("Erreur site aéroport:", e)

    vols_api = None
    if force or derniere_maj_api is None or (maintenant() - derniere_maj_api).total_seconds() >= FREQUENCE_API_LISTE_SECONDES:
        try:
            vols_api = recuperer_arrivees_aerodatabox()
            derniere_maj_api = maintenant()
        except Exception as e:
            print("Erreur API liste:", e)

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

    url = f"https://{RAPIDAPI_HOST}/flights/number/{numero}"
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST}
    try:
        r = requests.get(url, headers=headers, timeout=20)
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

    appels = 0
    for v in candidats:
        if appels >= MAX_LIVE_CALLS_PAR_CYCLE:
            break
        status = recuperer_live_status(v["numero"])
        appels += 1
        if status:
            v["live_status"] = status
    return vols


# =========================
# RÉSUMÉ ET ALERTES
# =========================

def niveau_affluence(nb30):
    if nb30 >= 8: return "🔴 Forte"
    if nb30 >= 4: return "🟠 Moyenne"
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
    )
    msg += bloc_terminal("🔵 T1", t1_30)
    msg += bloc_terminal("🟣 T2", t2_30)
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


def envoyer_alertes(vols):
    for v in vols:
        cle = cle_vol(v)
        status = v.get("site_status") or v.get("live_status") or v.get("status")

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
# BOUCLE
# =========================

def boucle_principale():
    global dernier_resume
    envoyer_telegram(
        "✅ <b>EasyTaxi Flight Alert V12 Hybride lancé</b>\n"
        "Site aéroport gratuit + API AeroDataBox optimisée."
    )

    try:
        vols = mettre_a_jour_cache_si_besoin(force=True)
        vols = enrichir_live_status(vols)
        initialiser_sans_spam(vols)
        envoyer_telegram(creer_resume(vols))
        dernier_resume = maintenant()
    except Exception as e:
        envoyer_telegram(f"⚠️ Erreur démarrage : {e}")

    while True:
        try:
            vols = mettre_a_jour_cache_si_besoin(force=False)
            vols = enrichir_live_status(vols)
            envoyer_alertes(vols)

            if dernier_resume is None or (maintenant() - dernier_resume).total_seconds() >= FREQUENCE_RESUME_SECONDES:
                envoyer_telegram(creer_resume(vols))
                dernier_resume = maintenant()

        except Exception as e:
            print("Erreur boucle:", e)
            envoyer_telegram(f"⚠️ Erreur EasyTaxi Flight Alert : {e}")

        time.sleep(10)


if __name__ == "__main__":
    boucle_principale()
