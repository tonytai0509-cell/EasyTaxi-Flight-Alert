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

state = {"offset": 0, "next_id": 1, "courses": {}, "wizards": {}, "bouton_id": None, "invitations": {}, "libres": {}}
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


def texte_copie(c):
    """Texte à copier/coller dans un autre groupe (sans le nom du donneur)."""
    l = [f"🕐 {c['heure']}", f"📍 {c['depart']}", f"🏁 {c['arrivee']}"]
    if c.get("commentaire"):
        l.append(f"💬 {c['commentaire']}")
    return "\n".join(l)[:256]


def lien_carte(adresse):
    """Petit lien 🗺 : ouvre l'adresse dans l'application de cartes."""
    url = "https://www.google.com/maps/search/?api=1&amp;query=" + urllib.parse.quote(adresse, safe="")
    return f' <a href="{url}">🗺</a>'


def texte_course(c):
    lignes = [
        f"🕐 <b>{echapper(c['heure'])}</b> · <i>{echapper(c['donneur_nom'])}, {c['poste_a']}</i>",
        f"📍 <b>{echapper(c['depart'])}</b>{lien_carte(c['depart'])}",
        f"🏁 <b>{echapper(c['arrivee'])}</b>{lien_carte(c['arrivee'])}",
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
         {"text": "📋 Copier", "copy_text": {"text": texte_copie(c)}}],
    ]}


def maj_carte(c):
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

<label for="heure">Heure</label>
<div class="ligne">
  <input id="heure" placeholder="Ex : 14h30" autocomplete="off" maxlength="40">
  <button type="button" class="maintenant" id="maintenant">Maintenant</button>
</div>

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
var $=function(id){return document.getElementById(id)};
function tap(){try{tg.HapticFeedback.impactOccurred("light");}catch(e){}}

/* Heure + bouton Maintenant */
var maintenant=false;
function majMaintenant(actif){
  maintenant=actif;
  $("maintenant").classList.toggle("actif",actif);
  if(actif){$("heure").value="";$("heure").placeholder="Maintenant";}
  else{$("heure").placeholder="Ex : 14h30";}
}
$("maintenant").addEventListener("click",function(){tap();majMaintenant(!maintenant);});
$("heure").addEventListener("input",function(){if(maintenant&&$("heure").value){majMaintenant(false);}});
$("heure").addEventListener("focus",function(){if(maintenant){majMaintenant(false);}});
majMaintenant(true); /* « Maintenant » coché d'office : on gagne un geste en roulant */

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
  function afficher(features){
    var vus={},items=[];
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
    if(q.length<3){cacher();return;}
    minuteur=setTimeout(function(){chercher(q);},260);
  });
  inp.addEventListener("blur",function(){setTimeout(cacher,150);});
}
suggestions("depart","liste-depart");
suggestions("arrivee","liste-arrivee");

/* Envoi */
function envoyer(){
  var d={heure:maintenant?"maintenant":$("heure").value.trim(),depart:$("depart").value.trim(),arrivee:$("arrivee").value.trim(),commentaire:$("commentaire").value.trim()};
  if(!d.heure||!d.depart||!d.arrivee){$("msg").textContent="Remplis l'heure, le départ et l'arrivée.";return;}
  $("ok").disabled=true;$("ok").textContent="Envoi…";$("msg").textContent="";
  d.initData=tg.initData;
  fetch("/api/course",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)})
    .then(function(r){return r.json();})
    .then(function(j){
      if(j.ok){try{tg.HapticFeedback.notificationOccurred("success");}catch(e){}tg.close();}
      else{$("msg").textContent=j.erreur||"Erreur, réessaie.";$("ok").disabled=false;$("ok").textContent="Envoyer au groupe";}
    })
    .catch(function(){$("msg").textContent="Pas de connexion, réessaie.";$("ok").disabled=false;$("ok").textContent="Envoyer au groupe";});
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
