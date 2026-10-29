#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Telegram de gestion de courses entre chauffeurs.

Fonctionnement :
  - Clavier fixe en bas du groupe : « 🚖 Donner une course » | « 🟢 Je suis libre » (commande /clavier).
  - Appui : le bot affiche 60 s un bouton « Ouvrir le formulaire » (mini-application : heure avec bouton
    Maintenant, départ, arrivée avec suggestions d'adresses, commentaire). Sans COURSES_APP_LINK, il pose
    les questions une par une dans le groupe.
  - Il poste une carte dans le groupe avec ✅ Accepter / ❌ Refuser.
  - Le premier qui accepte prend la course (la carte devient « Prise par X »).
  - Chaque refus ajoute une ligne « X ne peut pas ».
  - Tant que la course est libre : rappel toutes les 5 minutes.
  - Le donneur dispose de 🤝 Trouvé ailleurs (course réglée hors groupe) et 🗑 Annuler.

Variables d'environnement :
  COURSES_TOKEN                token du bot (BotFather)
  COURSES_CHAT_ID              id du groupe (ex : -1001234567890)
  DATA_DIR                     dossier de l'état persistant (défaut : .)
  COURSES_RAPPEL_SECONDES      délai entre rappels (défaut : 300)
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
RAPPEL_SECONDES = int(os.getenv("COURSES_RAPPEL_SECONDES", "300"))
EXPIRATION_MINUTES = int(os.getenv("COURSES_EXPIRATION_MINUTES", "120"))
WIZARD_TIMEOUT_SECONDES = int(os.getenv("COURSES_WIZARD_TIMEOUT", "180"))
CONSERVATION_JOURS = 7
PORT = int(os.getenv("PORT", "0") or 0)
APP_LINK = os.getenv("COURSES_APP_LINK", "").strip()
LOCK = threading.RLock()

TZ = ZoneInfo("Europe/Paris")
API = f"https://api.telegram.org/bot{TOKEN}"
BOUTON_DONNER = "🚖 Donner une course"
BOUTON_LIBRE = "🟢 Je suis libre"
BOUTON_POSITION = "📍 Me localiser"
POS_ACTIVE_SECONDS = 600   # position « en ligne » si mise à jour il y a moins de 10 min
POS_GARDE_SECONDS = 3600   # au-delà d'1 h sans mise à jour, on oublie la position
ASTUCE_SECONDS = 25        # durée d'affichage des petits messages position
RAPPEL_POSITION_SECONDS = 3 * 3600
LIBRE_SECONDS = 30 * 60
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

state = {"offset": 0, "next_id": 1, "courses": {}, "wizards": {}, "bouton_id": None, "invitations": {}, "libres": {}, "positions": {}, "astuces": {}, "pos_rappel": {}}
INVITATION_SECONDS = 60  # durée d'affichage du bouton « Ouvrir le formulaire »


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


def envoyer(texte, reply_markup=None, reply_to=None, silencieux=False):
    params = {"chat_id": CHAT_ID, "text": texte, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    if silencieux:
        params["disable_notification"] = True
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


def inviter_formulaire(user):
    """Appui sur le bouton fixe du bas : affiche (60 s) le bouton qui ouvre le formulaire."""
    uid = str(user["id"])
    ancienne = state["invitations"].pop(uid, None)
    if ancienne:
        supprimer(ancienne["id"])
    mid = envoyer(
        f"👇 {echapper(nom_affiche(user))}, voici le formulaire.",
        reply_markup={"inline_keyboard": [[{"text": "📝 Ouvrir le formulaire", "url": APP_LINK}]]},
        silencieux=True,
    )
    if mid:
        state["invitations"][uid] = {"id": mid, "ts": time.time()}


def declarer_libre(user):
    """Bouton « Je suis libre » : un petit message discret, remplacé si on rappuie, effacé après 30 min."""
    uid = str(user["id"])
    ancien = state["libres"].pop(uid, None)
    if ancien:
        supprimer(ancien["id"])
    mid = envoyer(f"🟢 <b>{echapper(nom_affiche(user))}</b> est libre", silencieux=True)
    if mid:
        state["libres"][uid] = {"id": mid, "ts": time.time()}


def retirer_libre(uid):
    ancien = state["libres"].pop(str(uid), None)
    if ancien:
        supprimer(ancien["id"])


def retirer_invitation(uid):
    inv = state["invitations"].pop(str(uid), None)
    if inv:
        supprimer(inv["id"])


def lien_bot():
    m = re.match(r"https?://t\.me/([^/?#]+)", APP_LINK or "")
    return f"https://t.me/{m.group(1)}" if m else ""


def lien_carte():
    if not APP_LINK:
        return ""
    return APP_LINK + ("&" if "?" in APP_LINK else "?") + "startapp=carte"


def position_active(uid):
    p = state["positions"].get(str(uid))
    return bool(p and time.time() - p["ts"] < POS_ACTIVE_SECONDS and time.time() < p["fin"] + 120)


def enregistrer_position(user, m, edite):
    loc = m.get("location") or {}
    if "latitude" not in loc or "longitude" not in loc:
        return
    date = m.get("date") or time.time()
    ts = m.get("edit_date") or date
    live = loc.get("live_period")
    fin = date + live if live else ts + 600
    if fin < time.time():
        return
    state["positions"][str(user["id"])] = {
        "nom": nom_affiche(user), "lat": loc["latitude"], "lon": loc["longitude"],
        "ts": ts, "fin": fin,
    }


def membre_du_groupe(uid):
    infos = (api("getChatMember", chat_id=CHAT_ID, user_id=uid).get("result")) or {}
    return infos.get("status") in ("creator", "administrator", "member") or (
        infos.get("status") == "restricted" and infos.get("is_member"))


def liste_positions():
    now = time.time()
    res = []
    for uid, p in state["positions"].items():
        if now > p["fin"] + 120 or now - p["ts"] > POS_GARDE_SECONDS:
            continue
        res.append({"id": uid, "nom": p["nom"], "lat": p["lat"], "lon": p["lon"],
                    "age": int(now - p["ts"]), "actif": now - p["ts"] < POS_ACTIVE_SECONDS})
    res.sort(key=lambda r: r["age"])
    return res


def envoyer_astuce(texte, markup=None):
    return envoyer(texte, reply_markup=markup, silencieux=True)


def aide_position(user):
    """Bouton « Me localiser » : explication courte + accès à la carte, effacée après quelques secondes."""
    uid = str(user["id"])
    ancien = state["astuces"].pop(uid, None)
    if ancien:
        supprimer(ancien["id"])
    if position_active(uid):
        texte = f"✅ {echapper(nom_affiche(user))} · position visible"
        duree = 10
    else:
        duree = ASTUCE_SECONDS
        texte = (f"📍 {echapper(nom_affiche(user))} : ouvre le bot en privé, puis 📎 → Position → "
                 f"<b>Partager en direct</b> (8 h). Tu apparais sur la carte.")
    boutons = []
    if lien_bot() and not position_active(uid):
        boutons.append({"text": "💬 Ouvrir le bot", "url": lien_bot()})
    if lien_carte():
        boutons.append({"text": "🗺️ Carte", "url": lien_carte()})
    mid = envoyer_astuce(texte, {"inline_keyboard": [boutons]} if boutons else None)
    if mid:
        state["astuces"][uid] = {"id": mid, "ts": time.time() - (ASTUCE_SECONDS - duree)}


def rappeler_position(user):
    """Après « Je suis libre » : un petit rappel, au plus une fois par 3 h, si la position n'est pas partagée."""
    uid = str(user["id"])
    if position_active(uid) or time.time() - state["pos_rappel"].get(uid, 0) < RAPPEL_POSITION_SECONDS:
        return
    state["pos_rappel"][uid] = time.time()
    ancien = state["astuces"].pop(uid, None)
    if ancien:
        supprimer(ancien["id"])
    boutons = [{"text": "📍 Comment ?", "callback_data": "posinfo:"}]
    mid = envoyer_astuce(f"📍 {echapper(nom_affiche(user))}, partage ta position pour apparaître sur la carte.",
                         {"inline_keyboard": [boutons]})
    if mid:
        state["astuces"][uid] = {"id": mid, "ts": time.time()}


def envoyer_clavier():
    """/clavier ou /start : pose le clavier fixe (Donner une course | Je suis libre) en bas du groupe."""
    supprimer(state.get("bouton_id"))
    state["bouton_id"] = None
    envoyer(
        "Boutons actifs en bas de l'écran.",
        reply_markup={
            "keyboard": [[{"text": BOUTON_DONNER}, {"text": BOUTON_LIBRE}]],
            "resize_keyboard": True,
            "is_persistent": True,
        },
        silencieux=True,
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


# Bouton Copier : Telegram affiche déjà son icône « deux carrés », on met un texte invisible.
# Si Telegram refuse un texte invisible, on retombe automatiquement sur le mot « Copier ».
COPIER_LABEL = "\u2800"


def texte_copie(c):
    """Texte à copier/coller dans un autre groupe (sans le nom du donneur)."""
    l = [f"🕐 {c['heure']}", f"📍 {c['depart']}", f"🏁 {c['arrivee']}"]
    if c.get("commentaire"):
        l.append(f"💬 {c['commentaire']}")
    return "\n".join(l)[:256]


def adresse_cliquable(adresse):
    """L'adresse elle-même est le lien : ouvre l'application de cartes."""
    url = "https://www.google.com/maps/search/?api=1&amp;query=" + urllib.parse.quote(adresse, safe="")
    return f'<a href="{url}">{echapper(adresse)}</a>'


def texte_course(c):
    lignes = [
        f"🕐 <b>{echapper(c['heure'])}</b> · <i>{echapper(c['donneur_nom'])}, {c['poste_a']}</i>",
        f"📍 <b>{adresse_cliquable(c['depart'])}</b>",
        f"🏁 <b>{adresse_cliquable(c['arrivee'])}</b>",
    ]
    if c.get("commentaire"):
        lignes.append(f"💬 {echapper(c['commentaire'])}")
    if c["refus"]:
        lignes.append("❌ " + ", ".join(echapper(n.split()[0]) for n in c["refus"].values() if n.strip()))
    statut = c["statut"]
    if statut == "prise":
        lignes.append(f"✅ <b>Prise par {echapper(c['pris_par_nom'])}</b>")
    elif statut == "trouvee":
        lignes.append("🤝 <b>Trouvée en dehors du groupe</b>")
    elif statut == "annulee":
        lignes.append("🗑 <b>Course annulée</b>")
    elif statut == "expiree":
        lignes.append("⌛ <b>Personne n'a pris cette course</b>")
    return "\n".join(lignes)


def clavier_course(c):
    if c["statut"] != "open":
        return {"inline_keyboard": []}
    cid = c["id"]
    return {"inline_keyboard": [
        [{"text": "✅ Accepter", "callback_data": f"acc:{cid}"},
         {"text": "❌ Refuser", "callback_data": f"ref:{cid}"}],
        [{"text": "🤝 Trouvé", "callback_data": f"found:{cid}"},
         {"text": "🗑 Annuler", "callback_data": f"cancel:{cid}"},
         {"text": COPIER_LABEL, "copy_text": {"text": texte_copie(c)}}],
    ]}


def maj_carte(c):
    global COPIER_LABEL
    d = api("editMessageText", chat_id=CHAT_ID, message_id=c["message_id"],
            text=texte_course(c), parse_mode="HTML", reply_markup=clavier_course(c),
            link_preview_options={"is_disabled": True})
    if not d.get("ok") and COPIER_LABEL != "Copier" and "not modified" not in str(d.get("description", "")):
        COPIER_LABEL = "Copier"
        api("editMessageText", chat_id=CHAT_ID, message_id=c["message_id"],
            text=texte_course(c), parse_mode="HTML", reply_markup=clavier_course(c),
            link_preview_options={"is_disabled": True})


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
        "demain": bool(w.get("demain")),
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
        global COPIER_LABEL
        if COPIER_LABEL != "Copier":
            COPIER_LABEL = "Copier"
            mid = envoyer(texte_course(c), reply_markup=clavier_course(c))
    if not mid:
        envoyer("⚠️ La course n'a pas pu être publiée, réessaie.")
        return
    c["message_id"] = mid
    state["courses"][cid] = c


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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#ffffff);--fg:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#7d8590);
--btn:var(--tg-theme-button-color,#2481cc);--btnfg:var(--tg-theme-button-text-color,#fff);--card:var(--tg-theme-secondary-bg-color,#f1f3f5);
--line:rgba(127,127,127,.25)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;padding:22px 18px 40px;background:var(--bg);color:var(--fg);font:16px/1.3 "Poppins",-apple-system,Roboto,"Segoe UI",sans-serif}
h1{margin:6px 0 4px;text-align:center;font-size:27px;font-weight:700;letter-spacing:.2px}
.trait{width:56px;height:4px;border-radius:4px;background:var(--btn);margin:10px auto 26px}
label{display:block;font-size:12px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:var(--hint);margin:18px 4px 7px}
input{width:100%;padding:15px 16px;font:500 18px "Poppins",-apple-system,Roboto,sans-serif;border:2px solid transparent;border-radius:14px;background:var(--card);color:var(--fg);outline:none;transition:border-color .15s}
input:focus{border-color:var(--btn)}
input::placeholder{color:var(--hint);opacity:.7;font-weight:400}
.ligne{display:flex;gap:10px}
.ligne input{flex:1;min-width:0}
.hm{flex:1;display:flex;align-items:center;gap:8px;min-width:0}
.hm span{font-size:26px;font-weight:600;color:var(--hint)}
.hm input{text-align:center;font-size:24px;font-weight:600;padding-left:0;padding-right:0}
.micro{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:38px;height:38px;border:0;border-radius:19px;background:transparent;font-size:20px;padding:0}
.micro.ecoute{background:rgba(229,72,77,.3)}
.jour{display:flex;gap:8px;margin-top:8px}
.jour button{flex:1;padding:9px;border:2px solid var(--line);border-radius:12px;background:transparent;color:var(--hint);font:600 15px "Poppins",-apple-system,Roboto,sans-serif}
.jour button.actif{border-color:var(--btn);background:var(--btn);color:var(--btnfg)}
.maintenant{flex:0 0 auto;padding:0 18px;border:2px solid var(--btn);border-radius:14px;background:transparent;color:var(--btn);font:600 16px "Poppins",-apple-system,Roboto,sans-serif}
.maintenant.actif{background:var(--btn);color:var(--btnfg)}
.champ{position:relative}
.liste{position:absolute;left:0;right:0;top:calc(100% + 6px);z-index:20;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 10px 28px rgba(0,0,0,.28);overflow:hidden;display:none}
.liste div{padding:12px 16px;border-top:1px solid var(--line);font-size:16px;font-weight:500}
.liste div:first-child{border-top:0}
.liste div small{display:block;color:var(--hint);font-size:13px;font-weight:400;margin-top:1px}
.liste div:active{background:var(--card)}
.envoyer{width:100%;margin-top:30px;padding:17px;font:600 18px "Poppins",-apple-system,Roboto,sans-serif;border:0;border-radius:14px;background:var(--btn);color:var(--btnfg)}
.envoyer:disabled{opacity:.55}
#msg{margin-top:14px;color:#e5484d;min-height:22px;font-size:15px;text-align:center}
</style></head><body>
<h1>Donner une course</h1>
<div class="trait"></div>

<label for="hh">Heure</label>
<div class="ligne">
  <div class="hm"><input id="hh" inputmode="numeric" pattern="[0-9]*" placeholder="--" autocomplete="off" maxlength="2"><span>:</span><input id="mm" inputmode="numeric" pattern="[0-9]*" placeholder="--" autocomplete="off" maxlength="2"></div>
  <button type="button" class="maintenant" id="maintenant">Maintenant</button>
</div>
<div class="jour"><button type="button" id="auj" class="actif">Aujourd'hui</button><button type="button" id="dem">Demain</button></div>

<label for="depart">Départ</label>
<div class="champ"><input id="depart" placeholder="Ex : Gare de Nice" autocomplete="off" maxlength="120"><div class="liste" id="liste-depart"></div></div>

<label for="arrivee">Arrivée</label>
<div class="champ"><input id="arrivee" placeholder="Ex : Aéroport Nice" autocomplete="off" maxlength="120"><div class="liste" id="liste-arrivee"></div></div>

<label for="commentaire">Commentaire (facultatif)</label>
<input id="commentaire" placeholder="Ex : 2 passagers, bagages" autocomplete="off" maxlength="120">

<button class="envoyer" id="ok" type="button">Envoyer au groupe</button>
<div id="msg"></div>

<script>
var tg=(window.Telegram&&window.Telegram.WebApp)||{ready:function(){},expand:function(){},close:function(){},initData:""};
try{tg.ready();tg.expand();}catch(e){}
try{if((tg.initDataUnsafe||{}).start_param==="carte"){location.replace("/carte"+location.hash);}}catch(e){}
var $=function(id){return document.getElementById(id)};
function tap(){try{tg.HapticFeedback.impactOccurred("light");}catch(e){}}

/* Heure + bouton Maintenant */
var maintenant=false;
function majMaintenant(actif){
  maintenant=actif;
  $("maintenant").classList.toggle("actif",actif);
  if(actif){$("hh").value="";$("mm").value="";}
}
$("maintenant").addEventListener("click",function(){tap();majMaintenant(!maintenant);});
/* Heure en deux cases HH : MM, clavier numérique, saut automatique vers les minutes */
function deux(v){v=String(v||"");return v.length<2?("0"+v).slice(-2):v;}
function getHeure(){
  var h=$("hh").value.replace(/[^0-9]/g,""),m=$("mm").value.replace(/[^0-9]/g,"");
  if(!h)return "";
  return deux(h)+"h"+deux(m||"00");
}
$("hh").addEventListener("input",function(){
  var v=this.value.replace(/[^0-9]/g,"").slice(0,2);
  if(v.length===1&&+v>2){v="0"+v;}            /* 9 -> 09 */
  if(v.length===2&&+v>23){v="23";}
  this.value=v;
  if(v&&maintenant){majMaintenant(false);this.value=v;}
  if(v.length===2){$("mm").focus();}
});
$("mm").addEventListener("input",function(){
  var v=this.value.replace(/[^0-9]/g,"").slice(0,2);
  if(v.length===1&&+v>5){v="0"+v;}            /* 7 -> 07 */
  if(v.length===2&&+v>59){v="59";}
  this.value=v;
  if(maintenant&&v){majMaintenant(false);this.value=v;}
});
$("hh").addEventListener("blur",function(){if(this.value.length===1){this.value=deux(this.value);}});
$("mm").addEventListener("blur",function(){if(this.value.length===1){this.value=deux(this.value);}});
["hh","mm"].forEach(function(id){$(id).addEventListener("focus",function(){if(maintenant){majMaintenant(false);}});});
majMaintenant(true); /* « Maintenant » coché d'office : on gagne un geste en roulant */

/* Aujourd'hui / Demain : pour demain, l'heure est obligatoire (pas de « Maintenant ») */
var demain=false;
function majJour(d){
  demain=d;
  $("auj").classList.toggle("actif",!d);
  $("dem").classList.toggle("actif",d);
  $("maintenant").style.display=d?"none":"";
  if(d){majMaintenant(false);$("hh").focus();}
  else{majMaintenant(true);}
}
$("auj").addEventListener("click",function(){tap();majJour(false);});
$("dem").addEventListener("click",function(){tap();majJour(true);});

/* Adresses : suggestions (facultatives, on peut toujours écrire librement) */
function suggestions(idInput,idListe){
  var inp=$(idInput),liste=$(idListe),minuteur=null,ctl=null;
  function cacher(){liste.style.display="none";liste.innerHTML="";}
  function libelle(f){
    var p=f.properties||{};
    var principal=p.name||p.street||"";
    var lieu=p.city||p.locality||p.district||p.county||"";
    if(!principal)return null;
    return {texte:principal+(lieu&&lieu!==principal?", "+lieu:""),principal:principal,lieu:(lieu&&lieu!==principal)?lieu:""};
  }
  function locaux(q){
    var n=sansAccents(q),res=[];
    if(n.length<2)return res;
    ETAB.forEach(function(e){
      var ok=e[1].some(function(a){return a===n||(n.length>=2&&a.indexOf(n)===0);})
        ||(n.length>=3&&sansAccents(e[0]).indexOf(n)>=0);
      if(ok){var p=e[0].split(", ");res.push({texte:e[0],principal:p[0],lieu:p.slice(1).join(", ")});}
    });
    return res.slice(0,3);
  }
  function afficher(features){
    var vus={},items=[];
    locaux(inp.value).forEach(function(l){vus[l.texte]=1;items.push(l);});
    (features||[]).forEach(function(f){
      var l=libelle(f);
      if(l&&!vus[l.texte]){vus[l.texte]=1;items.push(l);}
    });
    liste.innerHTML="";
    if(!items.length){cacher();return;}
    items.slice(0,5).forEach(function(l){
      var d=document.createElement("div");
      d.appendChild(document.createTextNode(l.principal));
      if(l.lieu){var s=document.createElement("small");s.textContent=l.lieu;d.appendChild(s);}
      d.addEventListener("pointerdown",function(e){e.preventDefault();inp.value=l.texte;cacher();tap();});
      liste.appendChild(d);
    });
    liste.style.display="block";
  }
  function chercher(q){
    if(ctl){try{ctl.abort();}catch(e){}}
    ctl=(window.AbortController?new AbortController():null);
    var url="https://photon.komoot.io/api/?lang=fr&limit=8&lat=43.70&lon=7.26&location_bias_scale=0.7&q="+encodeURIComponent(q);
    fetch(url,ctl?{signal:ctl.signal}:{}).then(function(r){return r.json();}).then(function(j){
      if(inp.value.trim()===q&&document.activeElement===inp)afficher(j.features);
    }).catch(function(){});
  }
  inp.addEventListener("input",function(){
    clearTimeout(minuteur);
    var q=inp.value.trim();
    if(q.length<2){cacher();return;}
    afficher([]);
    if(q.length<3)return;
    minuteur=setTimeout(function(){chercher(q);},260);
  });
  inp.addEventListener("blur",function(){setTimeout(cacher,150);});
}
suggestions("depart","liste-depart");
suggestions("arrivee","liste-arrivee");

/* Raccourcis à l'écrit : taper « pasteur », « tzanck », « T2 »... en sortant du champ le remplace par le nom complet */
var ETAB=[
 ["Hôpital Les Sources, Nice",["les sources","les sources b1","sources"]],
 ["Clinique Saint George, Nice",["saint george","saint georges","saint-george","saint-georges","csg","stg"]],
 ["Hôpital Pasteur, Nice",["pasteur","pasteur 2"]],
 ["Hôpital de l'Archet, Nice",["archet","archet 1","archet 2","l'archet"]],
 ["Hôpital Lenval, Nice",["lenval"]],
 ["Centre Antoine Lacassagne, Nice",["lacassagne","antoine lacassagne","cal"]],
 ["Clinique du Parc Impérial, Nice",["parc imperial","cpi"]],
 ["Clinique Saint-Antoine, Nice",["saint antoine","saint-antoine"]],
 ["Polyclinique Santa Maria, Nice",["santa maria"]],
 ["Clinique Saint-François, Nice",["saint francois","saint-francois"]],
 ["Hôpital Saint-Roch, Nice",["saint roch","saint-roch"]],
 ["Hôpital de Cimiez, Nice",["cimiez"]],
 ["Polyclinique Saint Jean, Cagnes-sur-Mer",["saint jean"]],
 ["Institut Arnault Tzanck, Saint-Laurent-du-Var",["tzanck","crc nice"]],
 ["Aéroport Nice Côte d'Azur - Terminal 1",["t1","terminal 1"]],
 ["Aéroport Nice Côte d'Azur - Terminal 2",["t2","terminal 2"]],
 ["Aéroport Nice Côte d'Azur",["aeroport","aeroport de nice","aeroport nice"]]
];
function sansAccents(t){return t.normalize("NFD").replace(/[\u0300-\u036f]/g,"").replace(/[’]/g,"'").toLowerCase().replace(/ +/g," ").trim();}
function resoudre(v){
  v=(v||"").trim();
  if(!v||/[0-9]{2,}/.test(v))return null;
  var n=sansAccents(v),exact=null,pref=[];
  ETAB.forEach(function(e){e[1].forEach(function(a){
    if(a===n){exact=e;}
    else if(n.length>=4&&a.indexOf(n)===0&&pref.indexOf(e)<0){pref.push(e);}
  });});
  if(exact)return exact[0];
  return pref.length===1?pref[0][0]:null;
}
["depart","arrivee"].forEach(function(id){
  $(id).addEventListener("blur",function(){var r=resoudre($(id).value);if(r)$(id).value=r;});
});

/* Dictée des adresses : bouton micro seulement si le navigateur sait écouter */
var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
function micro(id){
  if(!SR)return;
  var inp=$(id),bt=document.createElement("button"),rec=null;
  bt.type="button";bt.className="micro";bt.textContent="🎙";
  inp.parentNode.appendChild(bt);inp.style.paddingRight="52px";
  bt.addEventListener("click",function(){
    tap();
    if(rec){try{rec.stop();}catch(e){}return;}
    try{
      rec=new SR();rec.lang="fr-FR";rec.interimResults=false;rec.maxAlternatives=1;
      bt.classList.add("ecoute");
      rec.onresult=function(e){
        var t=((e.results[0]||[])[0]||{}).transcript||"";t=t.trim();
        if(t){inp.value=t.charAt(0).toUpperCase()+t.slice(1);var r=resoudre(inp.value);if(r)inp.value=r;}
      };
      rec.onerror=function(){$("msg").textContent="Micro indisponible ici : utilise le micro du clavier.";};
      rec.onend=function(){rec=null;bt.classList.remove("ecoute");};
      rec.start();
    }catch(x){rec=null;bt.classList.remove("ecoute");$("msg").textContent="Micro indisponible ici : utilise le micro du clavier.";}
  });
}
micro("depart");micro("arrivee");

/* Envoi */
function envoyer(){
  var d={demain:demain,heure:maintenant?"maintenant":getHeure(),depart:$("depart").value.trim(),arrivee:$("arrivee").value.trim(),commentaire:$("commentaire").value.trim()};
  d.depart=resoudre(d.depart)||d.depart;d.arrivee=resoudre(d.arrivee)||d.arrivee;
  if(demain&&!getHeure()){$("msg").textContent="Choisis l'heure de demain.";return;}
  if(!d.heure||!d.depart||!d.arrivee){$("msg").textContent="Remplis l'heure, le départ et l'arrivée.";return;}
  $("ok").disabled=true;$("ok").textContent="Envoi…";$("msg").textContent="";
  d.initData=tg.initData;
  fetch("/api/course",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)})
    .then(function(r){return r.json();})
    .then(function(j){
      if(j.ok){try{tg.HapticFeedback.notificationOccurred("success");}catch(e){}tg.close();setTimeout(function(){tg.close();},400);}
      else{$("msg").textContent=j.erreur||"Erreur, réessaie.";$("ok").disabled=false;$("ok").textContent="Envoyer au groupe";}
    })
    .catch(function(){$("msg").textContent="Pas de connexion, réessaie.";$("ok").disabled=false;$("ok").textContent="Envoyer au groupe";});
}
$("ok").addEventListener("click",envoyer);
/* Fermeture automatique après 2 min d'inactivité (évite la barre « Dispatch » oubliée en bas) */
var idleT=null;
function armerIdle(){clearTimeout(idleT);idleT=setTimeout(function(){tg.close();},120000);}
["touchstart","click","input","keydown","scroll"].forEach(function(e){document.addEventListener(e,armerIdle,{passive:true});});
armerIdle();
</script></body></html>"""


PAGE_CARTE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Carte</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--fg:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#7d8590);--card:var(--tg-theme-secondary-bg-color,#f1f3f5);--btn:var(--tg-theme-button-color,#2481cc)}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:15px/1.3 -apple-system,Roboto,"Segoe UI",sans-serif}
#app{display:flex;flex-direction:column;height:100%}
#map{flex:1;min-height:200px}
#haut{padding:12px 16px;font-weight:600;font-size:17px;display:flex;justify-content:space-between;background:var(--card)}
#haut span{color:var(--hint);font-weight:500;font-size:13px;align-self:center}
#liste{max-height:30%;overflow:auto;background:var(--card);padding:6px 16px 12px}
.l{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(127,127,127,.2)}
.l small{color:var(--hint)}
.pin{width:30px;height:30px;border-radius:50%;background:#2ea6ff;border:2px solid #fff;color:#fff;font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 4px rgba(0,0,0,.5)}
.pin.gris{background:#6b7a89}
.pin.moi{background:#2fbf71}
.lab{position:absolute;top:32px;left:50%;transform:translateX(-50%);background:rgba(14,22,33,.85);color:#fff;border-radius:10px;padding:1px 8px;font-size:12px;white-space:nowrap}
#vide{position:absolute;z-index:999;top:90px;left:20px;right:20px;text-align:center;background:var(--card);border-radius:12px;padding:12px;color:var(--hint);display:none}
</style></head><body>
<div id="app"><div id="haut">Carte des collègues<span id="nb"></span></div>
<div style="position:relative;flex:1;display:flex"><div id="map"></div><div id="vide">Personne ne partage sa position pour l'instant.</div></div>
<div id="liste"></div></div>
<script>
var tg=(window.Telegram&&window.Telegram.WebApp)||{ready:function(){},expand:function(){},initData:""};
try{tg.ready();tg.expand();}catch(e){}
var map=L.map("map",{zoomControl:false}).setView([43.70,7.26],12);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"}).addTo(map);
var marqueurs={},premier=true;
function age(s){return s<45?"à l'instant":s<3600?"il y a "+Math.max(1,Math.round(s/60))+" min":"il y a "+Math.round(s/3600)+" h";}
function esc(t){var d=document.createElement("div");d.textContent=t;return d.innerHTML;}
function rendre(j){
  var ps=j.positions||[],vus={},liste=document.getElementById("liste");liste.innerHTML="";
  ps.forEach(function(p,i){
    var cle=p.id;vus[cle]=1;
    var html='<div class="pin '+(p.actif?"":"gris")+'">'+esc(p.nom.charAt(0).toUpperCase())+'</div><div class="lab">'+esc(p.nom)+'</div>';
    var m=marqueurs[cle];
    if(m){m.setLatLng([p.lat,p.lon]);m.setIcon(L.divIcon({html:html,className:"",iconSize:[30,30],iconAnchor:[15,15]}));}
    else{marqueurs[cle]=L.marker([p.lat,p.lon],{icon:L.divIcon({html:html,className:"",iconSize:[30,30],iconAnchor:[15,15]})}).addTo(map);}
    var l=document.createElement("div");l.className="l";
    l.innerHTML="<span>"+(p.actif?"🟢 ":"⚪ ")+esc(p.nom)+"</span><small>"+age(p.age)+"</small>";
    l.onclick=function(){map.setView([p.lat,p.lon],15);};
    liste.appendChild(l);
  });
  Object.keys(marqueurs).forEach(function(c){if(!vus[c]){map.removeLayer(marqueurs[c]);delete marqueurs[c];}});
  document.getElementById("nb").textContent=ps.filter(function(p){return p.actif;}).length+" en ligne";
  document.getElementById("vide").style.display=ps.length?"none":"block";
  if(premier&&ps.length){premier=false;var b=L.latLngBounds(ps.map(function(p){return [p.lat,p.lon];}));map.fitBounds(b,{padding:[50,50],maxZoom:15});}
}
function charger(){
  fetch("/api/positions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({initData:tg.initData})})
   .then(function(r){return r.json();}).then(function(j){if(j.ok)rendre(j);else document.getElementById("vide").textContent="Ouvre la carte depuis le groupe.",document.getElementById("vide").style.display="block";})
   .catch(function(){});
}
charger();setInterval(charger,10000);
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
    demain = bool(corps.get("demain"))
    if demain and heure.lower() == "maintenant":
        return 400, {"ok": False, "erreur": "Choisis l'heure de demain."}
    heure = normaliser_heure(heure)
    if demain:
        heure = f"demain {heure}"
    w = {"uid": user["id"], "nom": nom_affiche(user), "heure": heure, "demain": demain,
         "depart": depart, "arrivee": arrivee, "commentaire": commentaire}
    with LOCK:
        publier_course(w)
        retirer_invitation(user["id"])
        retirer_libre(user["id"])
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
        if self.path.split("?")[0] == "/carte":
            self._repondre(200, PAGE_CARTE, "text/html; charset=utf-8")
        elif self.path.split("?")[0] in ("/", "/index.html"):
            self._repondre(200, PAGE_FORMULAIRE, "text/html; charset=utf-8")
        else:
            self._repondre(200, "ok", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path == "/api/positions":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                corps = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 10000 else {}
                user = valider_init_data(str(corps.get("initData", "")))
                if not user or not membre_du_groupe(user["id"]):
                    self._repondre(403, json.dumps({"ok": False}))
                    return
                with LOCK:
                    res = liste_positions()
                self._repondre(200, json.dumps({"ok": True, "positions": res, "moi": user["id"]}, ensure_ascii=False))
            except Exception:
                logger.exception("Positions")
                self._repondre(400, json.dumps({"ok": False}))
            return
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

def traiter_position(m, edite=False):
    """Position (en direct ou non) partagée dans le groupe ou en privé avec le bot."""
    chat = m.get("chat", {})
    user = m.get("from") or {}
    if user.get("is_bot") or not user.get("id") or not m.get("location"):
        return
    if str(chat.get("id")) == str(CHAT_ID):
        enregistrer_position(user, m, edite)
    elif chat.get("type") == "private" and membre_du_groupe(user["id"]):
        enregistrer_position(user, m, edite)
        if not edite:
            api("sendMessage", chat_id=chat["id"], text="✅ Position reçue. Tu apparais sur la carte du groupe.")


def message_prive(m):
    chat = m.get("chat", {})
    texte = (m.get("text") or "").strip()
    if texte.lower().startswith("/start"):
        boutons = []
        if lien_carte():
            boutons.append({"text": "🗺️ Voir la carte", "url": lien_carte()})
        api("sendMessage", chat_id=chat["id"], parse_mode="HTML",
            text="📍 Pour apparaître sur la carte du groupe : 📎 → <b>Position</b> → <b>Partager ma position en direct</b> (8 h).",
            **({"reply_markup": {"inline_keyboard": [boutons]}} if boutons else {}))


def traiter_message(m):
    chat = m.get("chat", {})
    if m.get("location"):
        traiter_position(m)
        return
    if chat.get("type") == "private":
        message_prive(m)
        return
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
    elif texte == BOUTON_LIBRE:
        supprimer(m["message_id"])
        declarer_libre(user)
    elif texte == BOUTON_DONNER:
        supprimer(m["message_id"])
        if APP_LINK:
            inviter_formulaire(user)
        else:
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

    if action == "posinfo":
        repondre_callback(cb_id, "Ouvre le bot en privé, puis 📎 → Position → Partager en direct (8 h). "
                                 "Tu peux aussi le faire dans le groupe.", alerte=True)
        return

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
        retirer_libre(uid)
        repondre_callback(cb_id, "Course acceptée ✅")
        # La carte affiche déjà « Prise par … » : pas de message en plus.
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
    elif u.get("edited_message"):
        em = u["edited_message"]
        if em.get("location"):
            traiter_position(em, edite=True)


# --------------------------------------------------------------------------- tâches de fond

def maintenance():
    """Saisies abandonnées, rappels, expiration, nettoyage. Retourne True si l'état a changé."""
    change = False
    now = time.time()
    for uid, inv in list(state["invitations"].items()):
        if now - inv["ts"] > INVITATION_SECONDS:
            supprimer(inv["id"])
            del state["invitations"][uid]
            change = True
    for uid, a in list(state["astuces"].items()):
        if now - a["ts"] > ASTUCE_SECONDS:
            supprimer(a["id"])
            del state["astuces"][uid]
            change = True
    for uid, p in list(state["positions"].items()):
        if now > p["fin"] + 120 or now - p["ts"] > POS_GARDE_SECONDS:
            del state["positions"][uid]
            change = True
    for uid, l in list(state["libres"].items()):
        if now - l["ts"] > LIBRE_SECONDS:
            supprimer(l["id"])
            del state["libres"][uid]
            change = True
    for cle, w in list(state["wizards"].items()):
        if now - w["debut"] > WIZARD_TIMEOUT_SECONDES:
            supprimer(w.get("prompt_id"))
            del state["wizards"][cle]
            change = True
    for cid, c in list(state["courses"].items()):
        if c["statut"] == "open" and c.get("demain"):
            # Course de demain : aucun rappel, c'est le donneur qui gère. Clôture silencieuse après 36 h.
            if now - c["cree_ts"] > 36 * 3600:
                cloturer(c, "expiree")
                change = True
        elif c["statut"] == "open":
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
    try:
        envoyer_clavier()
    except Exception:
        logger.exception("Envoi du clavier au démarrage impossible")
    while True:
        try:
            d = api("getUpdates", _timeout=25, offset=state["offset"], timeout=10,
                    allowed_updates=["message", "edited_message", "callback_query"])
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
