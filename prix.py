"""Estimation du prix d'une course en une phrase (« prix Pasteur pour 248 bd de Provence Saint-Laurent hdj »).

Mêmes règles que l'estimation de l'appli EasyTaxi (Estimation.kt / estimerMontantCourse) :
forfait 28 € si une adresse est à Nice (13 € sinon), 4 km inclus puis 1,27 €/km,
HDJ = retour à vide (+25 % sur les km, +50 % à partir de 50 km) sauf trajet entièrement
dans Nice, nuit (20 h – 8 h), samedi après-midi, dimanche et jours fériés = +50 %.
Distance par la route : service d'itinéraire de l'IGN (gratuit, sans clé), comme le site.
Ce n'est qu'une estimation, pas le montant exact CPAM.
"""

import re
import unicodedata

import requests

FORFAIT_NICE = 28.0
FORFAIT_HORS_NICE = 13.0
KM_INCLUS = 4.0
TARIF_KM = 1.27
SEUIL_VIDE = 50.0

# Raccourcis (même liste que le formulaire du site, js/etablissements.js) : [nom, raccourcis, adresse]
ETABLISSEMENTS = [
    ("Hôpital Pasteur 2", "pasteur p2 pst", "30 avenue de la Voie Romaine, 06000 Nice"),
    ("Hôpital de l'Archet", "archet arc a1 a2", "151 route Saint-Antoine de Ginestière, 06200 Nice"),
    ("Hôpital Lenval", "lenval len", "57 avenue de la Californie, 06200 Nice"),
    ("Hôpital Saint-Roch", "saint roch st roch str", "5 rue Pierre Dévoluy, 06000 Nice"),
    ("Hôpital de Cimiez", "cimiez cim", "4 avenue Reine Victoria, 06003 Nice"),
    ("Hôpital Les Sources", "sources src", "10 chemin René Pietruschi, 06100 Nice"),
    ("Centre Antoine Lacassagne", "lacassagne cal cac", "33 avenue de Valombrose, 06189 Nice"),
    ("Clinique Saint-George", "saint georges saint george st george st georges stg csg", "2 avenue de Rimiez, 06100 Nice"),
    ("Clinique du Parc Impérial", "parc imperial cpi", "28 boulevard du Tzarewitch, 06000 Nice"),
    ("Clinique Saint-Antoine", "saint antoine st antoine sta", "7 avenue Durante, 06000 Nice"),
    ("Clinique Saint-François", "saint francois st francois stf", "10 boulevard Pasteur, 06000 Nice"),
    ("Centre de Haute Énergie (CHE)", "che haute energie centre haute energie", "10 boulevard Pasteur, 06000 Nice"),
    ("Polyclinique Santa Maria", "santa maria psm", "57 avenue de la Californie, 06200 Nice"),
    ("Polyclinique Saint-Jean", "saint jean st jean stj", "92 avenue du Docteur Maurice Donat, 06800 Cagnes-sur-Mer"),
    ("Institut Arnault Tzanck", "tzanck tz iat", "231 avenue du Docteur Maurice Donat, 06721 Saint-Laurent-du-Var"),
    ("Aéroport Nice - Terminal 1", "aeroport t1", "Boulevard Maryse Bastié, 06200 Nice"),
    ("Aéroport Nice - Terminal 2", "aeroport t2", "Boulevard Jacqueline Auriol, 06200 Nice"),
    ("Gare de Nice-Ville", "gare gare nice", "Avenue Thiers, 06000 Nice"),
]

OPTIONS = {
    "hdj": "hdj",
    "nuit": "nuit", "dimanche": "nuit", "ferie": "nuit", "férié": "nuit", "weekend": "nuit", "we": "nuit",
    "ar": "ar", "a/r": "ar", "aller-retour": "ar", "allerretour": "ar",
}
# D'abord les séparateurs sûrs (« pour », « > »…), puis « à », « - » ou « / » s'il n'y en a pas.
SEPARATEURS = re.compile(r"\s+(?:pour|vers|jusqu'a|jusqu'à)\s+|\s*(?:->|→|=>|>|➡️|➡)\s*", re.IGNORECASE)
SEPARATEURS_FAIBLES = re.compile(r"\s+(?:à|-|/)\s+", re.IGNORECASE)
NICE = re.compile(r"\bnice\b", re.IGNORECASE)


def sans_accents(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").lower()


def est_demande(texte):
    return bool(re.match(r"^\s*/?prix\b", texte or "", re.IGNORECASE))


def analyser(texte):
    """« prix A pour B hdj nuit ar » -> dict (depart, arrivee, km, hdj, nuit, ar) ou message d'erreur (str)."""
    t = re.sub(r"^\s*/?prix(@\w+)?\b", "", texte, flags=re.IGNORECASE).strip()
    t = re.sub(r"aller\s+retour", "aller-retour", t, flags=re.IGNORECASE)
    opts = {"hdj": False, "nuit": False, "ar": False}
    mots = []
    for mot in t.split():
        cle = OPTIONS.get(sans_accents(mot).strip(".,;"))
        if cle:
            opts[cle] = True
        else:
            mots.append(mot)
    t = " ".join(mots)
    km = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*km\b", t, re.IGNORECASE)
    if m:
        km = float(m.group(1).replace(",", "."))
        t = (t[:m.start()] + t[m.end():]).strip()
    parties = [p.strip(" ,") for p in SEPARATEURS.split(t) if p and p.strip(" ,")]
    if len(parties) == 1:
        parties = [p.strip(" ,") for p in SEPARATEURS_FAIBLES.split(t) if p and p.strip(" ,")] or parties
    if len(parties) == 1:
        # Sans « pour » : on coupe là où un côté est un raccourci (« prix cagnes archet », « prix stg 12 rue x »).
        mots_t = parties[0].split()
        for i in range(1, len(mots_t)):
            g, d = " ".join(mots_t[:i]), " ".join(mots_t[i:])
            if raccourci(g) or raccourci(d):
                parties = [g, d]
                break
    if km is not None:
        # « prix 25 km nice hdj » : pas besoin d'adresses ; « nice » suffit pour le forfait.
        return {"km": km, "depart": t or "", "arrivee": "", **opts}
    if len(parties) != 2:
        return ("Je n'ai pas trouvé le départ et l'arrivée. Sépare-les par « pour » ou « > », par exemple :\n"
                "<code>prix Hôpital Pasteur pour 248 bd de Provence Saint-Laurent hdj</code>\n"
                "ou <code>prix 25 km nice nuit</code>")
    return {"km": None, "depart": parties[0], "arrivee": parties[1], **opts}


def raccourci(texte):
    cle = re.sub(r"[^a-z0-9' ]", " ", sans_accents(texte)).split()
    cle = " ".join(m for m in cle if m not in ("hopital", "clinique", "polyclinique", "centre", "institut", "de", "l", "la", "le", "nice"))
    if not cle:
        return None
    for nom, mots, adresse in ETABLISSEMENTS:
        if cle in sans_accents(mots).split() or f" {cle} " in f" {sans_accents(mots)} ":
            return nom, adresse
    return None


def localiser(texte, session=requests):
    """Texte -> (libellé lisible, ville, lon, lat) ou None. Raccourcis d'abord, puis adresse IGN (06 / 83 en priorité)."""
    r = raccourci(texte)
    requete, nom = (r[1], r[0]) if r else (texte, None)
    rep = session.get("https://data.geopf.fr/geocodage/search",
                      params={"q": requete, "limit": 5, "autocomplete": 0, "lat": 43.70, "lon": 7.25}, timeout=6)
    rep.raise_for_status()
    feats = rep.json().get("features") or []
    f = next((x for x in feats if str((x.get("properties") or {}).get("postcode", "")).startswith(("06", "83"))), None) or (feats[0] if feats else None)
    if not f:
        return None
    p = f.get("properties") or {}
    lon, lat = f["geometry"]["coordinates"]
    ville = p.get("city") or ""
    libelle = nom or p.get("label") or texte
    if nom and ville and ville.lower() not in nom.lower():
        libelle = f"{nom}, {ville}"
    return libelle, ville, lon, lat


def itineraire(a, b, session=requests):
    """(lon, lat) x2 -> (km, minutes) par la route."""
    rep = session.get("https://data.geopf.fr/navigation/itineraire", params={
        "resource": "bdtopo-osrm", "profile": "car", "optimization": "fastest",
        "start": f"{a[0]},{a[1]}", "end": f"{b[0]},{b[1]}",
        "distanceUnit": "kilometer", "timeUnit": "minute", "getSteps": "false",
    }, timeout=8)
    rep.raise_for_status()
    d = rep.json()
    km = float(d["distance"])
    if d.get("distanceUnit") == "meter":
        km /= 1000
    duree = float(d.get("duration") or 0)
    if d.get("timeUnit") == "second":
        duree /= 60
    elif d.get("timeUnit") == "hour":
        duree *= 60
    return km, round(duree)


def montant(km, nice, hdj_a_vide, majore):
    """Détail d'un trajet : (forfait, part_km, supplement_hdj, total), majorations comprises."""
    forfait = FORFAIT_NICE if nice else FORFAIT_HORS_NICE
    part_km = max(0.0, km - KM_INCLUS) * TARIF_KM
    supp = part_km * ((1.5 if km >= SEUIL_VIDE else 1.25) - 1) if hdj_a_vide else 0.0
    if majore:
        forfait, part_km, supp = forfait * 1.5, part_km * 1.5, supp * 1.5
    return forfait, part_km, supp, forfait + part_km + supp


def euros(v):
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def calculer(demande, session=requests):
    """Demande analysée -> dict prêt à afficher, ou message d'erreur (str)."""
    if demande["km"] is not None:
        km, duree = demande["km"], None
        dep, arr = f"Trajet de {km:g} km".replace(".", ",") + (" · Nice" if NICE.search(demande["depart"]) else ""), ""
        nice = bool(NICE.search(demande["depart"]))
        tout_nice = nice
    else:
        try:
            a = localiser(demande["depart"], session)
            b = localiser(demande["arrivee"], session)
        except Exception:
            return "Le service d'adresses ne répond pas, réessaie dans un instant."
        if not a:
            return f"Adresse de départ introuvable : « {demande['depart']} ». Ajoute la ville."
        if not b:
            return f"Adresse d'arrivée introuvable : « {demande['arrivee']} ». Ajoute la ville."
        try:
            km, duree = itineraire(a[2:], b[2:], session)
        except Exception:
            return "Le calcul d'itinéraire ne répond pas, réessaie dans un instant."
        dep, arr = a[0], b[0]
        n1, n2 = bool(NICE.search(a[1] or a[0])), bool(NICE.search(b[1] or b[0]))
        nice, tout_nice = n1 or n2, n1 and n2
    a_vide = demande["hdj"] and not tout_nice
    return {"depart": dep, "arrivee": arr, "km": km, "duree": duree, "nice": nice, "tout_nice": tout_nice,
            "hdj": demande["hdj"], "a_vide": a_vide, "nuit": demande["nuit"], "ar": demande["ar"]}


def texte_resultat(r, echapper):
    """Réponse du bot (HTML Telegram)."""
    fois = 2 if r["ar"] else 1
    f, k, s, total = montant(r["km"], r["nice"], r["a_vide"], r["nuit"])
    # Aller-retour : l'aller est facturé normalement, le retour porte le retour à vide HDJ (comme l'appli).
    if r["ar"]:
        total = montant(r["km"], r["nice"], False, r["nuit"])[3] + total
    titre = f"{echapper(r['depart'])} → {echapper(r['arrivee'])}" if r["arrivee"] else echapper(r["depart"])
    options = [o for o, ok in (("HDJ", r["hdj"]), ("nuit / dimanche / férié", r["nuit"]), ("aller-retour", r["ar"])) if ok]
    distance = f"{r['km']:.1f} km".replace(".", ",") + (f" · {r['duree']} min" if r.get("duree") else "")
    lignes = [
        f"💶 <b>{titre}</b>",
        *( [f"<i>{', '.join(options)}</i>"] if options else [] ),
        f"\n<b>{euros(total)}</b>" + (" <i>(aller + retour)</i>" if r["ar"] else ""),
        "",
        f"📏 Distance : {distance}" + (" (par trajet)" if r["ar"] else ""),
        f"🏙️ Forfait {'Nice' if r['nice'] else 'hors Nice'} : {euros(f)}",
        f"➕ Km au-delà de 4 ({max(0, r['km'] - KM_INCLUS):.1f} × 1,27 €) : {euros(k)}".replace(".", ",", 1),
    ]
    if s:
        lignes.append(f"🏥 HDJ retour à vide +{50 if r['km'] >= SEUIL_VIDE else 25} % : {euros(s)}")
    elif r["hdj"] and r["tout_nice"]:
        lignes.append("🏥 HDJ : pas de retour à vide (trajet entièrement dans Nice)")
    if r["nuit"]:
        lignes.append("🌙 Majoration +50 % comprise")
    else:
        nuit = montant(r["km"], r["nice"], r["a_vide"], True)[3]
        if r["ar"]:
            nuit += montant(r["km"], r["nice"], False, True)[3]
        lignes.append(f"🌙 De nuit / dimanche / férié : {euros(nuit)}")
    if r["hdj"] and r["a_vide"]:
        sans = montant(r["km"], r["nice"], False, r["nuit"])[3] * fois
        lignes.append(f"Sans HDJ : {euros(sans)}")
    lignes.append("\n<i>Estimation, pas le montant exact CPAM.</i>")
    return "\n".join(lignes)
