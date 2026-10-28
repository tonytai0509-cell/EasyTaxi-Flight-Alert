#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Telegram de gestion de courses entre chauffeurs.

Fonctionnement :
  - Un bouton permanent « 🚖 Donner une course » s'affiche en bas du groupe (commande /clavier).
  - Le bot pose les questions une par une : heure, départ, arrivée, commentaire (facultatif).
  - Il poste une carte dans le groupe avec ✅ Accepter / ❌ Refuser.
  - Le premier qui accepte prend la course (la carte devient « Prise par X »).
  - Chaque refus ajoute une ligne « X ne peut pas ».
  - Tant que la course est libre : rappel toutes les 2 minutes.
  - Le donneur dispose de 🤝 Trouvé ailleurs (course réglée hors groupe) et 🗑 Annuler.

Variables d'environnement :
  COURSES_TOKEN                token du bot (BotFather)
  COURSES_CHAT_ID              id du groupe (ex : -1001234567890)
  DATA_DIR                     dossier de l'état persistant (défaut : .)
  COURSES_RAPPEL_SECONDES      délai entre rappels (défaut : 120)
  COURSES_EXPIRATION_MINUTES   arrêt des rappels si personne ne prend la course (défaut : 120)
  COURSES_WIZARD_TIMEOUT       délai max d'une saisie abandonnée, en secondes (défaut : 180)
  COURSES_APP_LINK             lien direct de la mini-application (ex : https://t.me/MonBot/course).
                               Si défini : le bouton du groupe ouvre un formulaire à 3 cases + commentaire.
  PORT                         port du petit serveur web du formulaire (fourni par Railway)
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TOKEN = os.getenv("COURSES_TOKEN")
CHAT_ID = os.getenv("COURSES_CHAT_ID")
DATA_DIR = os.getenv("DATA_DIR", ".")
STATE_FILE = os.getenv("COURSES_STATE_FILE", os.path.join(DATA_DIR, "courses_state.json"))
RAPPEL_SECONDES = int(os.getenv("COURSES_RAPPEL_SECONDES", "120"))
EXPIRATION_MINUTES = int(os.getenv("COURSES_EXPIRATION_MINUTES", "120"))
WIZARD_TIMEOUT_SECONDES = int(os.getenv("COURSES_WIZARD_TIMEOUT", "180"))
CONSERVATION_JOURS = 7
PORT = int(os.getenv("PORT", "0") or 0)
APP_LINK = os.getenv("COURSES_APP_LINK", "").strip()
LOCK = threading.RLock()

TZ = ZoneInfo("Europe/Paris")
API = f"https://api.telegram.org/bot{TOKEN}"
BOUTON_DONNER = "🚖 Donner une course"
MAX_CHAMP = 120

ORDRE_ETAPES = ["heure", "depart", "arrivee", "commentaire"]
QUESTIONS = {
    "heure": "⏰ À quelle heure ? <i>(ex : 14h30, ou « maintenant »)</i>",
    "depart": "📍 Départ ?",
    "arrivee": "🏁 Arrivée ?",
    "commentaire": "💬 Commentaire <i>(facultatif)</i> : écris-le, ou appuie sur Passer.",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("courses_bot")

state = {"offset": 0, "next_id": 1, "courses": {}, "wizards": {}, "bouton_id": None}


# --------------------------------------------------------------------------- état

def charger_etat():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state.update({k: data[k] for k in state if k in data})
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Lecture de l'état impossible, démarrage à vide : {e}")


def sauver_etat():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"Sauvegarde de l'état impossible : {e}")


# --------------------------------------------------------------------------- Telegram

def api(method, _timeout=30, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=_timeout)
        data = r.json()
        if not data.get("ok") and "not modified" not in str(data.get("description", "")):
            logger.warning(f"{method} refusé : {data.get('description')}")
        return data
    except Exception as e:
        logger.error(f"{method} en erreur : {e}")
        return {"ok": False}


def envoyer(texte, reply_markup=None, reply_to=None):
    params = {"chat_id": CHAT_ID, "text": texte, "parse_mode": "HTML"}
    if reply_markup:
        params["reply_markup"] = reply_markup
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    d = api("sendMessage", **params)
    return (d.get("result") or {}).get("message_id") if d.get("ok") else None


def supprimer(message_id):
    if message_id:
        api("deleteMessage", chat_id=CHAT_ID, message_id=message_id)


def repondre_callback(cb_id, texte=None, alerte=False):
    params = {"callback_query_id": cb_id}
    if texte:
        params["text"] = texte
        params["show_alert"] = alerte
    api("answerCallbackQuery", **params)


def poster_bouton():
    """Poste le bouton qui ouvre le formulaire, en supprimant l'ancien pour qu'il soit toujours en bas."""
    supprimer(state.get("bouton_id"))
    state["bouton_id"] = envoyer(
        "🚖 Pour donner une course, appuie sur le bouton ci-dessous.",
        reply_markup={"inline_keyboard": [[{"text": BOUTON_DONNER, "url": APP_LINK}]]},
    )


def envoyer_clavier():
    if APP_LINK:
        # retire l'ancien clavier du bas, puis poste le bouton qui ouvre le formulaire
        supprimer(envoyer("🚖", reply_markup={"remove_keyboard": True}))
        poster_bouton()
        return
    envoyer(
        "🚖 Le bouton <b>Donner une course</b> est actif en bas de l'écran.",
        reply_markup={
            "keyboard": [[{"text": BOUTON_DONNER}]],
            "resize_keyboard": True,
            "is_persistent": True,
        },
    )


# --------------------------------------------------------------------------- textes

def echapper(t):
    return html.escape(str(t or ""), quote=False)


def nettoyer_champ(t):
    return " ".join((t or "").split())[:MAX_CHAMP]


def nom_affiche(user):
    return nettoyer_champ(user.get("first_name") or user.get("username") or "Quelqu'un")


def mention(uid, nom):
    return f'<a href="tg://user?id={uid}">{echapper(nom)}</a>'


def normaliser_heure(txt):
    t = txt.strip()
    m = re.fullmatch(r"(\d{1,2})\s*[hH:]\s*(\d{2})?", t)
    if m and int(m.group(1)) < 24 and (not m.group(2) or int(m.group(2)) < 60):
        return f"{int(m.group(1))}h{m.group(2) or '00'}"
    m = re.fullmatch(r"(\d{2})(\d{2})", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1))}h{m.group(2)}"
    return t


def texte_course(c):
    lignes = [
        f"🚖 <b>COURSE</b> · 🕐 <b>{echapper(c['heure'])}</b>",
        f"📍 Départ : <b>{echapper(c['depart'])}</b>",
        f"🏁 Arrivée : <b>{echapper(c['arrivee'])}</b>",
    ]
    if c.get("commentaire"):
        lignes.append(f"💬 {echapper(c['commentaire'])}")
    lignes.append(f"<i>Donnée par {echapper(c['donneur_nom'])} · postée à {c['poste_a']}</i>")
    for nom in c["refus"].values():
        lignes.append(f"❌ {echapper(nom)} ne peut pas")
    statut = c["statut"]
    if statut == "prise":
        lignes.append(f"\n✅ <b>Prise par {echapper(c['pris_par_nom'])}</b>")
    elif statut == "trouvee":
        lignes.append("\n🤝 <b>Trouvée en dehors du groupe</b>")
    elif statut == "annulee":
        lignes.append("\n🗑 <b>Course annulée</b>")
    elif statut == "expiree":
        lignes.append("\n⌛ <b>Personne n'a pris cette course</b>")
    return "\n".join(lignes)


def clavier_course(c):
    if c["statut"] != "open":
        return {"inline_keyboard": []}
    cid = c["id"]
    return {"inline_keyboard": [
        [{"text": "✅ Accepter", "callback_data": f"acc:{cid}"},
         {"text": "❌ Refuser", "callback_data": f"ref:{cid}"}],
        [{"text": "🤝 Trouvé ailleurs", "callback_data": f"found:{cid}"},
         {"text": "🗑 Annuler", "callback_data": f"cancel:{cid}"}],
    ]}


def maj_carte(c):
    api("editMessageText", chat_id=CHAT_ID, message_id=c["message_id"],
        text=texte_course(c), parse_mode="HTML", reply_markup=clavier_course(c))


# --------------------------------------------------------------------------- courses

def cloturer(c, statut):
    c["statut"] = statut
    supprimer(c.get("rappel_id"))
    c["rappel_id"] = None
    maj_carte(c)


def envoyer_rappel(c):
    supprimer(c.get("rappel_id"))
    c["dernier_rappel_ts"] = time.time()
    c["rappel_id"] = envoyer(
        f"⏰ <b>Course toujours libre</b> · {echapper(c['heure'])}\n"
        f"{echapper(c['depart'])} → {echapper(c['arrivee'])}\n"
        f"<i>Donnée par {echapper(c['donneur_nom'])}</i>",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Accepter", "callback_data": f"acc:{c['id']}"},
            {"text": "❌ Refuser", "callback_data": f"ref:{c['id']}"},
        ]]},
        reply_to=c["message_id"],
    )


def publier_course(w):
    cid = str(state["next_id"])
    state["next_id"] += 1
    c = {
        "id": cid,
        "donneur_id": w["uid"],
        "donneur_nom": w["nom"],
        "heure": w["heure"],
        "depart": w["depart"],
        "arrivee": w["arrivee"],
        "commentaire": w.get("commentaire", ""),
        "poste_a": datetime.now(TZ).strftime("%H:%M"),
        "cree_ts": time.time(),
        "dernier_rappel_ts": time.time(),
        "statut": "open",
        "pris_par_id": None,
        "pris_par_nom": None,
        "refus": {},
        "message_id": None,
        "rappel_id": None,
    }
    mid = envoyer(texte_course(c), reply_markup=clavier_course(c))
    if not mid:
        envoyer("⚠️ La course n'a pas pu être publiée, réessaie.")
        return
    c["message_id"] = mid
    state["courses"][cid] = c
    if APP_LINK:
        poster_bouton()


# --------------------------------------------------------------------------- saisie guidée

def envoyer_question(w):
    etape = w["step"]
    boutons = [{"text": "❌ Annuler", "callback_data": f"wcancel:{w['uid']}"}]
    if etape == "commentaire":
        boutons.insert(0, {"text": "⏭ Passer", "callback_data": f"wskip:{w['uid']}"})
    w["prompt_id"] = envoyer(
        f"🚖 <b>Course de {echapper(w['nom'])}</b>\n{QUESTIONS[etape]}",
        reply_markup={"inline_keyboard": [boutons]},
    )


def demarrer_saisie(user):
    uid = user["id"]
    ancien = state["wizards"].pop(str(uid), None)
    if ancien:
        supprimer(ancien.get("prompt_id"))
    w = {"uid": uid, "nom": nom_affiche(user), "step": "heure", "debut": time.time(), "prompt_id": None}
    state["wizards"][str(uid)] = w
    envoyer_question(w)


def terminer_saisie(w):
    state["wizards"].pop(str(w["uid"]), None)
    publier_course(w)


def avancer_saisie(w, texte):
    supprimer(w.get("prompt_id"))
    valeur = nettoyer_champ(texte)
    etape = w["step"]
    w[etape] = normaliser_heure(valeur) if etape == "heure" else valeur
    w["debut"] = time.time()
    suivante = ORDRE_ETAPES.index(etape) + 1
    if suivante < len(ORDRE_ETAPES):
        w["step"] = ORDRE_ETAPES[suivante]
        envoyer_question(w)
    else:
        terminer_saisie(w)



# --------------------------------------------------------------------------- mini-application (formulaire)

PAGE_FORMULAIRE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Donner une course</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--fg:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#777);
--btn:var(--tg-theme-button-color,#2481cc);--btnfg:var(--tg-theme-button-text-color,#fff);--card:var(--tg-theme-secondary-bg-color,#f1f1f1)}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:17px/1.3 -apple-system,Roboto,Segoe UI,sans-serif}
h1{font-size:20px;margin:0 0 16px}
label{display:block;font-size:14px;color:var(--hint);margin:14px 0 6px}
input{width:100%;padding:14px;font-size:18px;border:2px solid transparent;border-radius:12px;background:var(--card);color:var(--fg);outline:none}
input:focus{border-color:var(--btn)}
button{width:100%;margin-top:22px;padding:16px;font-size:18px;font-weight:600;border:0;border-radius:12px;background:var(--btn);color:var(--btnfg)}
button:disabled{opacity:.5}
#msg{margin-top:14px;color:#d33;min-height:22px;font-size:15px}
</style></head><body>
<h1>🚖 Donner une course</h1>
<label for="heure">⏰ Heure</label>
<input id="heure" placeholder="14h30 ou maintenant" autocomplete="off" maxlength="40">
<label for="depart">📍 Départ</label>
<input id="depart" placeholder="Ex : Gare de Nice" autocomplete="off" maxlength="120">
<label for="arrivee">🏁 Arrivée</label>
<input id="arrivee" placeholder="Ex : Aéroport T2" autocomplete="off" maxlength="120">
<label for="commentaire">💬 Commentaire (facultatif)</label>
<input id="commentaire" placeholder="Ex : 2 passagers, bagages" autocomplete="off" maxlength="120">
<button id="ok">✅ Envoyer au groupe</button>
<div id="msg"></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const $=id=>document.getElementById(id);
async function envoyer(){
  const d={heure:$("heure").value.trim(),depart:$("depart").value.trim(),arrivee:$("arrivee").value.trim(),commentaire:$("commentaire").value.trim()};
  if(!d.heure||!d.depart||!d.arrivee){$("msg").textContent="Remplis l'heure, le départ et l'arrivée.";return;}
  $("ok").disabled=true;$("msg").textContent="";
  try{
    const r=await fetch("/api/course",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({initData:tg.initData,...d})});
    const j=await r.json();
    if(j.ok){tg.close();}else{$("msg").textContent=j.erreur||"Erreur, réessaie.";$("ok").disabled=false;}
  }catch(e){$("msg").textContent="Pas de connexion, réessaie.";$("ok").disabled=false;}
}
$("ok").addEventListener("click",envoyer);
</script></body></html>"""


def valider_init_data(init_data):
    """Vérifie la signature Telegram du formulaire et retourne l'utilisateur, ou None."""
    try:
        d = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recu = d.pop("hash", None)
        if not recu:
            return None
        controle = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calcule = hmac.new(secret, controle.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calcule, recu):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(d.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def creer_course_depuis_formulaire(corps):
    """Retourne (code HTTP, réponse JSON)."""
    user = valider_init_data(str(corps.get("initData", "")))
    if not user:
        return 403, {"ok": False, "erreur": "Formulaire non reconnu. Rouvre-le depuis le groupe."}
    membre = api("getChatMember", chat_id=CHAT_ID, user_id=user["id"])
    infos = membre.get("result") or {}
    if infos.get("status") not in ("creator", "administrator", "member") and not (
            infos.get("status") == "restricted" and infos.get("is_member")):
        return 403, {"ok": False, "erreur": "Réservé aux membres du groupe."}
    heure = nettoyer_champ(str(corps.get("heure", "")))
    depart = nettoyer_champ(str(corps.get("depart", "")))
    arrivee = nettoyer_champ(str(corps.get("arrivee", "")))
    commentaire = nettoyer_champ(str(corps.get("commentaire", "")))
    if not (heure and depart and arrivee):
        return 400, {"ok": False, "erreur": "Remplis l'heure, le départ et l'arrivée."}
    w = {"uid": user["id"], "nom": nom_affiche(user), "heure": normaliser_heure(heure),
         "depart": depart, "arrivee": arrivee, "commentaire": commentaire}
    with LOCK:
        publier_course(w)
        sauver_etat()
    return 200, {"ok": True}


class FormulaireHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _repondre(self, code, contenu, type_="application/json; charset=utf-8"):
        octets = contenu.encode("utf-8") if isinstance(contenu, str) else contenu
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(octets)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(octets)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            self._repondre(200, PAGE_FORMULAIRE, "text/html; charset=utf-8")
        else:
            self._repondre(200, "ok", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/course":
            self._repondre(404, json.dumps({"ok": False}))
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 10000:
                raise ValueError
            corps = json.loads(self.rfile.read(n).decode("utf-8"))
            code, rep_ = creer_course_depuis_formulaire(corps)
        except Exception:
            logger.exception("Formulaire invalide")
            code, rep_ = 400, {"ok": False, "erreur": "Requête invalide."}
        self._repondre(code, json.dumps(rep_, ensure_ascii=False))


def demarrer_serveur_web():
    if not PORT:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), FormulaireHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"Formulaire web en écoute sur le port {PORT}.")


# --------------------------------------------------------------------------- événements

def traiter_message(m):
    chat = m.get("chat", {})
    if str(chat.get("id")) != str(CHAT_ID):
        if chat.get("type") in ("group", "supergroup"):
            logger.warning(f"Message ignoré d'un autre groupe : id={chat.get('id')} ({chat.get('title')})")
        return
    user = m.get("from") or {}
    texte = (m.get("text") or "").strip()
    if user.get("is_bot") or not user.get("id") or not texte:
        return
    commande = texte.split()[0].split("@")[0].lower() if texte.startswith("/") else None
    if commande in ("/clavier", "/start"):
        supprimer(m["message_id"])
        envoyer_clavier()
    elif texte == BOUTON_DONNER:
        supprimer(m["message_id"])
        if not APP_LINK:
            demarrer_saisie(user)
    elif commande is None:
        w = state["wizards"].get(str(user["id"]))
        if w:
            supprimer(m["message_id"])
            avancer_saisie(w, texte)


def traiter_callback(cb):
    cb_id = cb["id"]
    user = cb.get("from") or {}
    uid = user.get("id")
    nom = nom_affiche(user)
    msg = cb.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
        repondre_callback(cb_id)
        return
    action, _, ref = (cb.get("data") or "").partition(":")

    if action in ("wcancel", "wskip"):
        w = state["wizards"].get(str(uid))
        if str(uid) != ref:
            repondre_callback(cb_id, "Ce n'est pas ta saisie.")
        elif not w:
            repondre_callback(cb_id, "Saisie expirée.")
            supprimer(msg.get("message_id"))
        elif action == "wcancel":
            state["wizards"].pop(str(uid), None)
            supprimer(w.get("prompt_id"))
            repondre_callback(cb_id, "Saisie annulée.")
        else:
            supprimer(w.get("prompt_id"))
            repondre_callback(cb_id)
            terminer_saisie(w)
        return

    c = state["courses"].get(ref)
    if not c:
        repondre_callback(cb_id, "Course introuvable.")
        return
    if c["statut"] != "open":
        if c["statut"] == "prise":
            repondre_callback(cb_id, f"Déjà prise par {c['pris_par_nom']}.", alerte=True)
        else:
            repondre_callback(cb_id, "Cette course est clôturée.", alerte=True)
        return

    if action == "acc":
        if uid == c["donneur_id"]:
            repondre_callback(cb_id, "C'est ta propre course.")
            return
        c["refus"].pop(str(uid), None)
        c["pris_par_id"], c["pris_par_nom"] = uid, nom
        cloturer(c, "prise")
        repondre_callback(cb_id, "Course acceptée ✅")
        envoyer(
            f"✅ {mention(c['donneur_id'], c['donneur_nom'])}, <b>{echapper(nom)}</b> a pris ta course "
            f"de {echapper(c['heure'])} ({echapper(c['depart'])} → {echapper(c['arrivee'])}).",
            reply_to=c["message_id"],
        )
    elif action == "ref":
        if uid == c["donneur_id"]:
            repondre_callback(cb_id, "Tu es le donneur : utilise Annuler.")
        elif str(uid) in c["refus"]:
            repondre_callback(cb_id, "Tu as déjà refusé.")
        else:
            c["refus"][str(uid)] = nom
            repondre_callback(cb_id, "Refus enregistré.")
            maj_carte(c)
    elif action in ("found", "cancel"):
        if uid != c["donneur_id"]:
            repondre_callback(cb_id, "Seul le donneur peut faire ça.", alerte=True)
            return
        cloturer(c, "trouvee" if action == "found" else "annulee")
        repondre_callback(cb_id, "C'est noté.")
    else:
        repondre_callback(cb_id)


def traiter_update(u):
    if u.get("callback_query"):
        traiter_callback(u["callback_query"])
    elif u.get("message"):
        traiter_message(u["message"])


# --------------------------------------------------------------------------- tâches de fond

def maintenance():
    """Saisies abandonnées, rappels, expiration, nettoyage. Retourne True si l'état a changé."""
    change = False
    now = time.time()
    for cle, w in list(state["wizards"].items()):
        if now - w["debut"] > WIZARD_TIMEOUT_SECONDES:
            supprimer(w.get("prompt_id"))
            del state["wizards"][cle]
            change = True
    for cid, c in list(state["courses"].items()):
        if c["statut"] == "open":
            if now - c["cree_ts"] > EXPIRATION_MINUTES * 60:
                cloturer(c, "expiree")
                envoyer(
                    f"⌛ {mention(c['donneur_id'], c['donneur_nom'])}, personne n'a pris ta course de "
                    f"{echapper(c['heure'])} ({echapper(c['depart'])} → {echapper(c['arrivee'])}).",
                    reply_to=c["message_id"],
                )
                change = True
            elif now - c["dernier_rappel_ts"] >= RAPPEL_SECONDES:
                envoyer_rappel(c)
                change = True
        elif now - c["cree_ts"] > CONSERVATION_JOURS * 86400:
            del state["courses"][cid]
            change = True
    return change


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("COURSES_TOKEN et COURSES_CHAT_ID sont obligatoires.")
    charger_etat()
    demarrer_serveur_web()
    logger.info("Bot de courses démarré.")
    while True:
        try:
            d = api("getUpdates", _timeout=25, offset=state["offset"], timeout=10,
                    allowed_updates=["message", "callback_query"])
            if not d.get("ok"):
                time.sleep(3)
                continue
            updates = d.get("result", [])
            with LOCK:
                for u in updates:
                    state["offset"] = u["update_id"] + 1
                    try:
                        traiter_update(u)
                    except Exception:
                        logger.exception("Erreur de traitement d'un update")
                if maintenance() or updates:
                    sauver_etat()
        except Exception:
            logger.exception("Erreur dans la boucle principale")
            time.sleep(5)


if __name__ == "__main__":
    main()
