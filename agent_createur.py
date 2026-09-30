#!/usr/bin/env python3
"""
AGENT CREATEUR (v3.3)
=====================
Rôle : va chercher dans Airtable la prochaine tâche au statut "à faire",
génère le contenu avec Groq, fabrique un PDF propre, puis remet la tâche à
jour avec le statut "à valider". Toi seul valides ou refuses ensuite.

NOUVEAUTÉS v3.0
- Prompts réécrits : français, structure imposée, sans tableau ni emoji.
- Pack de prompts généré en 3 appels (5 prompts chacun) : plus de profondeur.
- Contrôle qualité à deux niveaux : une structure inutilisable est refusée (rien
  n'est écrit dans Airtable, la tâche reste "à faire") ; un défaut mineur
  déclenche un 2e essai avec consignes précises, puis la meilleure version est gardée.
- PDF (couverture, sommaire, cartes de prompts) joint au champ Airtable "PDF".

CE QUE CET AGENT NE FAIT JAMAIS (sécurité) :
- Il ne publie rien sur Maketou/Payhip/Gumroad/Etsy.
- Il ne touche à aucun compte de paiement ou de retrait.
- Il ne traite qu'UNE tâche par exécution (protège tes quotas).

REPO PUBLIC : les logs GitHub sont lisibles par tout le monde.
Ce script n'y écrit donc jamais : clés, titres de produits, contenus,
adresses ou identifiants Airtable. Le PDF n'est ni commité ni publié en
"artifact" GitHub : il part uniquement vers ton Airtable privé.

Toutes les clés viennent de variables d'environnement (Secrets GitHub).
Fichiers du dépôt : agent_createur.py, mise_en_page.py, requirements.txt.
"""

import base64
import difflib
import os
import re
import sys
import time
import unicodedata
from urllib.parse import quote

import requests

VERSION = "3.3"
_DEBUT = time.monotonic()
BUDGET_TOTAL_SECONDES = 270   # le workflow coupe à 5 min : on s'arrête proprement avant


# --- Configuration (lue depuis GitHub, jamais en dur) ---
def _env(nom):
    """Lit une variable en retirant espaces et retours à la ligne invisibles
    (très fréquents après un copier-coller sur téléphone)."""
    return (os.environ.get(nom) or "").strip()


GROQ_API_KEY = _env("GROQ_API_KEY")
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = _env("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = _env("AIRTABLE_TABLE") or "Produits"
CHAMP_PDF = _env("AIRTABLE_CHAMP_PDF") or "PDF"     # champ Airtable de type Attachment
MARQUE = _env("MARQUE")                             # optionnel : nom affiché sur le PDF

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Modèles gratuits Groq. Limites relevées en août 2026 (guide tiers reprenant la doc Groq) :
# 30 requêtes/min, 1 000 requêtes/jour, 8 000 tokens/min, 200 000 tokens/jour, par modèle.
# La page https://console.groq.com/settings/limits de TON compte fait foi.
# Chaque modèle a son propre quota : si le 1er est saturé, on passe au 2e.
# Pour changer de modèle sans toucher au code : variable GitHub GROQ_MODELS
# (liste séparée par des virgules).
MODELES_PAR_DEFAUT = "openai/gpt-oss-120b,openai/gpt-oss-20b"

MAX_TOKENS_PARTIE = 3800      # une partie du pack (5 prompts) : réflexion + texte
MAX_TOKENS_DOCUMENT = 5000    # CV ou template Notion, en un seul appel
ATTENTE_MAX_SECONDES = 90     # au-delà, on ne patiente pas : modèle suivant
MAX_RETRIES_429 = 2
LONGUEUR_MIN_CONTENU = 200    # en dessous, on considère la réponse ratée
TAILLE_MAX_PDF_OCTETS = 4_500_000   # Airtable accepte 5 Mo par envoi

NB_PARTIES_PACK = 3
NB_PROMPTS_PAR_PARTIE = 5
ESSAIS_PAR_MODELE = 2         # 2e essai (avec consignes précises) si le contenu est refusé ou imparfait


# ======================================================================
# TEXTES FIXES DES PRODUITS (modifiables ici, sans risque pour le reste)
# ======================================================================
MODE_EMPLOI_PACK = """## Comment utiliser ce pack

1. **Choisissez** le prompt qui correspond à votre situation (voir le sommaire).
2. **Copiez** le bloc de texte en entier.
3. **Remplacez** chaque [VARIABLE] par vos informations : plus vous donnez de contexte, meilleur est le résultat.
4. **Collez** le tout dans ChatGPT, Claude, Gemini ou Le Chat, puis **relisez et ajustez** avant tout envoi à un client.

### Bonnes pratiques

- **Protégez les données** : ne collez jamais de mots de passe, de coordonnées bancaires ni d’informations confidentielles de vos clients dans une IA.
- **Relisez toujours** : l’IA peut se tromper ou inventer. Vous restez responsable de ce que vous envoyez.
- **Affinez** : si le résultat ne convient pas, précisez ce qui manque (« plus court », « ton plus direct ») au lieu de tout recommencer.
- **Personnalisez** : gardez vos versions préférées de chaque prompt, elles deviendront votre bibliothèque."""

MENTIONS_PACK = """## Mentions

- **Points de départ** : ces prompts sont des modèles à adapter. Les résultats produits par une IA doivent toujours être relus et vérifiés.
- **Conseil professionnel** : les sujets juridiques, fiscaux ou comptables ne remplacent pas l’avis d’un professionnel.
- **Licence d’utilisation** : ce document est destiné à un usage personnel ou professionnel. Merci de ne pas le revendre ni le redistribuer tel quel."""

BANNIERE_CV = ("> **Exemples fictifs :** tous les noms, chiffres et références de ce document "
               "sont inventés à titre d’illustration. Remplacez-les par vos informations réelles.")

BANNIERE_NOTION = ("> **Document de travail :** plan de construction du template. "
                   "Le produit à vendre est le template Notion que l’on duplique, pas ce document.")

# ======================================================================
# PROMPTS DE GÉNÉRATION
# ======================================================================
SYSTEME_PACK = """Tu es rédacteur senior et expert en prompt engineering. Tu crées des packs de prompts vendus comme produit numérique à des Assistants Virtuels (VA) et Online Business Managers (OBM) freelances francophones. Le lecteur a payé : chaque prompt doit être précis, immédiatement utilisable et nettement meilleur que ce qu'il écrirait lui-même en 30 secondes.

RÈGLES ABSOLUES
1. Français professionnel et sans faute. Aucun emoji (même pour conseiller d'en utiliser), aucun tableau dans ta réponse, aucun HTML.
2. Aucune introduction, aucune conclusion, aucun commentaire sur ta réponse : uniquement le contenu demandé.
3. Chaque prompt est écrit pour être collé tel quel dans ChatGPT, Claude, Gemini ou Le Chat. Il suit le schéma RÔLE / CONTEXTE / TÂCHE / CONTRAINTES / FORMAT DE SORTIE (ces mots exacts, en majuscules, suivis de « : »), tutoie l'IA (« Tu es... ») et commence la ligne TÂCHE par un verbe à l'impératif (« Rédige », « Crée », « Propose », jamais à l'infinitif). Il demande à l'IA de poser au plus 3 questions si une information essentielle manque.
4. Les variables à remplacer s'écrivent en MAJUSCULES entre crochets, par exemple [NOM DU CLIENT]. Chaque prompt contient de 3 à 6 variables. N'utilise jamais les crochets pour autre chose.
5. Aucun nom de personne, d'entreprise ou de marque réels dans les exemples, aucun chiffre ni résultat promis. Cite uniquement des outils réels et actuels (Notion, Trello, Google Workspace, Zoom, Canva, Calendly...) et n'attribue à un outil que des fonctions qu'il possède réellement (Calendly = prise de rendez-vous ; Notion et Trello = suivi de tâches ; Zoom = visioconférence) ; en cas de doute, reste générique (« votre agenda »).
6. Sécurité : ne demande jamais de saisir un mot de passe, un code d'accès, un IBAN ou une donnée personnelle sensible dans un prompt. Pour partager des accès, recommande un gestionnaire de mots de passe.
7. Si un prompt touche au juridique, à la fiscalité ou à la comptabilité, précise dans « Astuce » que le résultat doit être validé par un professionnel.
8. Les 5 prompts d'une partie sont tous différents : varie les livrables (message, document, checklist, plan, script, analyse).
9. Les lignes « Quand l'utiliser », « Résultat attendu » et « Astuce » s'adressent au lecteur : vouvoie-le (« Vérifiez... », « Ajoutez... »). Seuls les prompts tutoient l'IA.
10. Le lecteur travaille seul(e) : n'écris ni « l'agence » ni « l'équipe » ; utilise [MON NOM] ou [MA MARQUE] quand il faut le désigner.
11. Ne demande jamais à l'IA d'inventer un témoignage, un avis client, une statistique, une référence ou une citation : elle n'utilise que les informations fournies dans les variables. N'écris jamais de lien ni d'adresse fictifs : utilise une variable comme [LIEN DU FORMULAIRE].
12. Cohérence : les nombres annoncés dans « Résultat attendu » (mots, lignes, colonnes, étapes, minutes) doivent correspondre exactement aux consignes du prompt, et les durées doivent être réalistes (un appel de lancement dure 30 à 60 minutes). Quand le prompt fixe un maximum, « Résultat attendu » écrit « jusqu'à N » ou « N au maximum », jamais « N » seul."""

UTILISATEUR_PACK = """Thème du pack : « {titre} ».

Tu rédiges la PARTIE {numero} sur {total} : « {axe_nom} » - {axe_desc}.

Titres déjà utilisés dans les autres parties (ne les répète pas, ne les reformule pas) : {deja}

Écris exactement ceci, dans cet ordre :

1) Une ligne de titre de catégorie, adaptée au thème (3 à 7 mots), qui commence par « ## ».

2) Cinq prompts, chacun construit exactement ainsi :

### Titre court et concret (3 à 8 mots, sans numéro)
**Quand l'utiliser :** une phrase.
```
RÔLE : ...
CONTEXTE : ... [VARIABLE] ...
TÂCHE : ...
CONTRAINTES : ...
FORMAT DE SORTIE : ...
```
**Résultat attendu :** une à deux phrases qui décrivent ce que l'IA va produire (structure, longueur).
**Astuce :** une phrase (variante utile ou erreur à éviter).

Chaque prompt (le texte entre les deux lignes ```) fait entre 120 et 180 mots : le CONTEXTE demande au moins 3 informations au lecteur (sous forme de variables) et les CONTRAINTES donnent 3 à 5 précisions concrètes (longueur, ton, ce qu'il faut éviter, structure). Écris la catégorie puis les 5 prompts, rien d'autre."""

AXES_PACK = [
    ("Avant : préparer et cadrer",
     "tout ce qu'il faut clarifier, préparer ou organiser avant d'agir"),
    ("Pendant : produire et communiquer",
     "rédiger, échanger et exécuter concrètement avec les clients ou prospects"),
    ("Après : suivre, relancer et fidéliser",
     "suivre les résultats, relancer, conclure, faire le bilan et entretenir la relation"),
]

SYSTEME_CV = """Tu es un expert en recrutement freelance et en rédaction de profils pour Assistants Virtuels (VA) et Online Business Managers (OBM) francophones. Tu écris un produit numérique payant : chaque section doit être concrète et directement réutilisable.

RÈGLES ABSOLUES
1. Français professionnel et sans faute, vouvoiement du lecteur. Aucun emoji, aucun tableau, aucun HTML. Utilise uniquement les niveaux de titre « ## » et « ### », des listes à puces et des citations (« > »).
2. Aucune introduction ni conclusion, aucun commentaire sur ta réponse.
3. Tous les exemples sont FICTIFS et signalés comme tels. Les informations à remplacer s'écrivent en MAJUSCULES entre crochets, par exemple [SPÉCIALITÉ]. N'invente ni certification, ni témoignage, ni chiffre présenté comme réel.
4. N'écris aucune date ni année précise ; pour la disponibilité, utilise [DATE DE DISPONIBILITÉ].
5. Cite uniquement des outils, plateformes et certifications réels et actuels (Make, Zapier, Notion, Trello, Canva, Google Workspace, Meta Blueprint...). En cas de doute, reste générique (« un outil d'automatisation »).
6. Les limites de longueur que tu annonces doivent correspondre exactement aux exemples que tu fournis.
7. Aucune promesse de résultat garanti."""

UTILISATEUR_CV = """Crée le contenu d'un template de profil professionnel sur le thème : « {titre} ».
Public : VA/OBM qui veulent décrocher des clients (Upwork, LinkedIn, sites de freelance, prospection directe).

Structure exacte, avec ces titres de niveau « ## » :

## Comment utiliser ce template
3 à 4 phrases, puis une liste de 3 étapes.

## Structure du profil
Liste numérotée des sections dans l'ordre (8 à 10 sections) : nom de la section, puis son rôle en une phrase.

## Modèle section par section
Pour chaque section de la structure, un titre « ### Nom de la section » suivi de :
**Modèle à compléter :** un texte type avec des [VARIABLES].
**Exemple fictif :** une citation « > » réaliste et convaincante.

## Trois accroches réutilisables
Trois accroches de 2 à 3 phrases, chacune dans une citation « > », pour trois plateformes différentes.

## Sept conseils pour se démarquer
Liste à puces, une phrase par conseil, concrète et actionnable.

## Erreurs fréquentes à éviter
Liste à puces, 5 erreurs.

## Checklist avant de publier votre profil
Liste de cases « - [ ] », 8 à 10 points de contrôle.

Longueur totale : 1 300 à 1 800 mots."""

SYSTEME_NOTION = """Tu es un consultant Notion expert et un rédacteur produit. Tu conçois des templates Notion vendus à des Assistants Virtuels (VA) et Online Business Managers (OBM) francophones. Ton plan doit permettre de construire le template sans rien deviner.

RÈGLES ABSOLUES
1. Français professionnel et sans faute. Aucun emoji, aucun tableau, aucun HTML. Utilise uniquement les niveaux de titre « ## » et « ### », des listes à puces et des citations (« > »).
2. Aucune introduction ni conclusion, aucun commentaire sur ta réponse.
3. N'utilise que des types de propriétés Notion qui existent : Titre, Texte, Nombre, Sélection, Sélection multiple, Statut, Date, Personne, Fichiers et médias, Case à cocher, URL, E-mail, Téléphone, Formule, Relation, Rollup (Cumul), Date de création. N'invente ni propriété, ni fonction.
4. Une formule n'est proposée que si elle est simple et exacte : syntaxe Notion prop("Nom de la propriété"), avec toujours une phrase qui décrit ce qu'elle calcule. Sinon, préfère une Relation avec un Rollup.
5. Les données d'exemple sont fictives. N'écris aucune date ni année figée : utilise des dates relatives (« aujourd'hui + 3 jours »).
6. L'acheteur reçoit un lien qu'il duplique dans son Notion : n'écris jamais « installez les bases », « copiez-collez les vues » ni rien qui suppose une construction par l'acheteur.
7. N'annonce que ce que le template fait réellement. Pas d'automatisation ni de synchronisation qui n'existent pas dans ta conception."""

UTILISATEUR_NOTION = """Conçois le plan d'un template Notion sur le thème : « {titre} ».
Public : VA/OBM qui gèrent plusieurs clients et veulent paraître professionnels.
Contraintes : 3 bases de données au maximum, 2 à 3 vues par base, un template simple à prendre en main en 10 minutes.

Structure exacte, avec ces titres de niveau « ## » :

## Fiche du template
Nom, promesse en 2 phrases orientées bénéfice client, pour qui, ce qu'il contient, ce qu'il ne fait pas (2 limites honnêtes).

## Bases de données
Pour chaque base : un titre « ### Nom de la base », puis une liste à puces « Nom de la propriété (Type) : rôle en quelques mots ».

## Relations et calculs
Liste à puces : chaque relation, chaque rollup, chaque formule (avec sa syntaxe et ce qu'elle calcule).

## Vues à créer
Pour chaque base, une liste à puces : nom de la vue, type (Tableau, Kanban, Calendrier, Chronologie, Liste, Galerie), filtre, tri, regroupement.

## Données d'exemple
Pour chaque base, 3 entrées fictives réalistes en liste à puces, avec dates relatives.

## Page d'accueil du template (texte à coller)
Le texte que l'acheteur lira en haut du template : bienvenue chaleureuse, comment dupliquer le template dans son espace Notion, 5 étapes de prise en main, 3 astuces.

## Ordre de construction (pour le créateur)
Checklist « - [ ] » avec le temps estimé de chaque étape.

Longueur totale : 1 400 à 1 900 mots."""

# Types de produits pris en charge (les 3 formats validés pour la niche VA/OBM)
TYPES_PRODUITS = ("notion_template", "prompt_pack", "cv_template")

# Contrôle qualité minimal des documents en un seul appel
EXIGENCES_DOCUMENT = {
    "cv_template": {"systeme": SYSTEME_CV, "utilisateur": UTILISATEUR_CV,
                    "sections": 6, "caracteres": 4000, "minimum": 1500, "banniere": BANNIERE_CV},
    "notion_template": {"systeme": SYSTEME_NOTION, "utilisateur": UTILISATEUR_NOTION,
                        "sections": 6, "caracteres": 4000, "minimum": 1500, "banniere": BANNIERE_NOTION},
}

# Présentation du PDF selon le type de produit
PRESENTATION_PDF = {
    "prompt_pack": {"etiquette": "Pack de prompts", "legende_code": "Prompt à copier-coller",
                    "saut_avant_partie": True},
    "cv_template": {"etiquette": "Template de profil professionnel",
                    "sous_titre": "Modèles à compléter, exemples fictifs et checklist pour décrocher des clients"},
    "notion_template": {"etiquette": "Template Notion",
                        "sous_titre": "Plan de construction et texte de la page d'accueil (document de travail)"},
}


# ======================================================================
# GROQ
# ======================================================================
class ErreurModele(Exception):
    """Ce modèle ne peut pas répondre (ou sa réponse est refusée) : on essaie le suivant."""


class ErreurFatale(Exception):
    """Erreur qui ne se règle pas en changeant de modèle : on arrête tout."""


class ErreurGeneration(Exception):
    """Aucun modèle n'a pu produire un contenu valide : la tâche reste 'à faire'."""


def liste_modeles():
    brut = os.environ.get("GROQ_MODELS") or MODELES_PAR_DEFAUT
    return [m.strip() for m in brut.split(",") if m.strip()]


def _temps_restant():
    return BUDGET_TOTAL_SECONDES - (time.monotonic() - _DEBUT)


def _lire_retry_after(response):
    try:
        return float(response.headers.get("retry-after", 20))
    except (TypeError, ValueError):
        return 20.0


def _message_groq(response):
    """Message d'erreur Groq (ne contient ni clé ni donnée perso)."""
    try:
        err = response.json().get("error", {})
        return f"{err.get('type', 'inconnu')}: {err.get('message', '')}"[:250]
    except (ValueError, AttributeError):
        return "réponse illisible"


def appeler_groq(modele, systeme, prompt, max_tokens):
    """Appelle UN modèle Groq. Renvoie le texte, ou lève ErreurModele/ErreurFatale."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if systeme:
        messages.append({"role": "system", "content": systeme})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": modele,
        "messages": messages,
        "temperature": 0.6,
        "max_completion_tokens": max_tokens,
        "reasoning_effort": "low",     # peu de réflexion = plus de place pour le texte
        "include_reasoning": False,
    }

    for tentative in range(MAX_RETRIES_429 + 1):
        if _temps_restant() < 25:
            raise ErreurFatale("délai global presque épuisé : arrêt propre, la tâche reste 'à faire'")
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
        except requests.exceptions.RequestException as e:
            raise ErreurModele(f"problème réseau ({type(e).__name__})")

        if r.status_code == 429:
            attente = _lire_retry_after(r)
            if attente > ATTENTE_MAX_SECONDES or tentative == MAX_RETRIES_429:
                raise ErreurModele("limite Groq atteinte (429)")
            if attente + 25 > _temps_restant():
                raise ErreurFatale("délai global presque épuisé : arrêt propre, la tâche reste 'à faire'")
            print(f"  -> Limite Groq atteinte, pause de {int(attente)}s...")
            time.sleep(attente)
            continue

        if r.status_code in (401, 403):
            raise ErreurFatale("clé Groq refusée (401/403) : vérifie le secret GROQ_API_KEY")
        if r.status_code in (400, 404, 413):
            raise ErreurModele(f"requête refusée ({r.status_code}) {_message_groq(r)}")
        if r.status_code >= 500:
            raise ErreurModele(f"erreur serveur Groq ({r.status_code})")
        if not r.ok:
            raise ErreurModele(f"réponse inattendue (HTTP {r.status_code})")

        try:
            choix = r.json()["choices"][0]
            contenu = (choix["message"].get("content") or "").strip()
            fin = choix.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError):
            raise ErreurModele("réponse Groq illisible")

        if fin == "length":
            raise ErreurModele("réponse coupée (limite de longueur atteinte)")
        if len(contenu) < LONGUEUR_MIN_CONTENU:
            raise ErreurModele("réponse vide ou trop courte")
        return contenu

    raise ErreurModele("échec après plusieurs tentatives")


# ======================================================================
# NETTOYAGE ET CONTRÔLE QUALITÉ DU TEXTE GÉNÉRÉ
# ======================================================================
_RE_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")
_ZERO_LARGEUR = dict.fromkeys(map(ord, "\u200b\u200c\ufeff\u2060"), None)
_RE_H4 = re.compile(r"(?m)^#{4,6}\s+(.*?)\s*$")
_RE_APOSTROPHE = re.compile(r"(?<=[A-Za-zÀ-ÿ])'(?=[A-Za-zÀ-ÿ])")
_RE_SEP_TABLEAU = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RE_VARIABLE = re.compile(r"\[[^\[\]\n]{2,80}\]")
_MOTS_EN = re.compile(r"\b(the|and|you|your|with|for|this|that|are|will|please)\b", re.I)
_MOTS_FR = re.compile(r"\b(le|la|les|des|et|pour|vous|de|du|un|une|est|sur|dans|que|qui|je|tu)\b", re.I)


def _retirer_emoji(texte):
    """Retire les emoji, puis répare la ponctuation qu'ils laissent : « (✅, ⏳, ❌) » ne doit pas devenir « (, , ) »."""
    t = _RE_EMOJI.sub("", texte)
    if t == texte:
        return t
    t = re.sub(r"\(\s*(?:[,;]\s*)+", "(", t)
    t = re.sub(r"(?:\s*[,;])+\s*\)", ")", t)
    t = re.sub(r"(?<=\S)\s*,(?:\s*,)+", ",", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\([ \t]+", "(", t)
    t = re.sub(r"[ \t]+([,.)])", r"\1", t)
    return re.sub(r"(?<=\S)[ \t]{2,}", " ", t)


def _cellules(ligne):
    t = ligne.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [c.strip() for c in t.split("|")]


def tableaux_vers_listes(markdown):
    """Airtable et le PDF n'affichent pas les tableaux : chaque ligne devient une puce."""
    lignes = markdown.split("\n")
    sortie, i, dans_code = [], 0, False
    while i < len(lignes):
        ligne = lignes[i]
        if ligne.strip().startswith("```"):
            dans_code = not dans_code
            sortie.append(ligne)
            i += 1
            continue
        if (not dans_code and "|" in ligne and i + 1 < len(lignes)
                and "|" in lignes[i + 1] and _RE_SEP_TABLEAU.match(lignes[i + 1])):
            entetes = _cellules(ligne)
            i += 2
            while i < len(lignes) and lignes[i].strip() and "|" in lignes[i]:
                cellules = _cellules(lignes[i])
                premier = cellules[0] if cellules else ""
                autres = []
                for k, valeur in enumerate(cellules[1:], start=1):
                    if not valeur:
                        continue
                    nom = entetes[k] if k < len(entetes) and entetes[k] else ""
                    autres.append(f"{nom} : {valeur}" if nom else valeur)
                puce = f"- **{premier}**" if premier else "-"
                if autres:
                    puce += " : " + " ; ".join(autres)
                sortie.append(puce)
                i += 1
            continue
        sortie.append(ligne)
        i += 1
    return "\n".join(sortie)


def _retirer_enveloppe_code(texte):
    """Certains modèles enveloppent toute leur réponse dans un bloc ```markdown ... ```."""
    lignes = texte.strip().split("\n")
    if (len(lignes) > 2 and lignes[-1].strip() == "```"
            and re.match(r"^```\s*(markdown|md|text)?\s*$", lignes[0].strip(), re.I)):
        return "\n".join(lignes[1:-1])
    return texte


def nettoyer_markdown(texte):
    """Rend le texte propre et prévisible : sans emoji, sans tableau, sans \\n littéraux."""
    t = texte.replace("\r\n", "\n").replace("\r", "\n")
    t = _retirer_enveloppe_code(t)
    t = t.replace("\\n", "\n")
    t = t.translate(_ZERO_LARGEUR)
    t = t.replace("\u2011", "-").replace("\u202f", "\u00a0")
    t = _retirer_emoji(t)
    t = _RE_APOSTROPHE.sub("\u2019", t)          # apostrophe typographique : l'IA -> l’IA
    t = "\n".join(l.rstrip() for l in t.split("\n"))
    t = tableaux_vers_listes(t)
    t = _RE_H4.sub(r"**\1**", t)                 # Airtable ne gère que les titres # ## ###
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _sans_accents(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def _semble_anglais(texte):
    return len(_MOTS_EN.findall(texte)) > len(_MOTS_FR.findall(texte))


# --- Pack de prompts : lecture d'une partie (1 catégorie + 5 prompts) ---
# Deux niveaux : ErreurModele = structure inutilisable (on réessaie, puis on change de modèle) ;
# avertissement = contenu utilisable mais imparfait (2e essai avec consignes précises, puis on
# garde la meilleure version : tu relis de toute façon avant de valider).
_ETIQUETTES = {
    "quand": re.compile(r"^\s*(?:[-*]\s+)?\**\s*quand\s+l['’]utiliser\s*\**\s*:?\s*\**\s*(.*)$", re.I),
    "resultat": re.compile(r"^\s*(?:[-*]\s+)?\**\s*r[ée]sultat\s+attendu\s*\**\s*:?\s*\**\s*(.*)$", re.I),
    "astuce": re.compile(r"^\s*(?:[-*]\s+)?\**\s*astuce\s*\**\s*:?\s*\**\s*(.*)$", re.I),
}
_CHAMPS_PROMPT = (r"ROLE", r"CONTEXTE", r"TACHE", r"CONTRAINTES?", r"FORMAT(?:\s+DE\s+SORTIE)?")
_RE_ACCOLADES2 = re.compile(r"\{\{\s*([^{}\n]{2,70}?)\s*\}\}")
_RE_ACCOLADES1 = re.compile(r"\{\s*([A-ZÀ-ÖØ-Ý][^{}\n]{1,68}?)\s*\}")
_RE_ANGLES = re.compile(r"<\s*([A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý0-9 '’/_\-]{1,68}?)\s*>")


def _champ(lignes, cle):
    """Texte d'une étiquette (« Quand l'utiliser », « Résultat attendu », « Astuce »),
    quelle que soit la façon dont le modèle a mis le gras et les deux-points."""
    for k, ligne in enumerate(lignes):
        m = _ETIQUETTES[cle].match(ligne)
        if not m:
            continue
        morceaux = [m.group(1).strip()]
        for suite in lignes[k + 1:]:
            if (not suite.strip() or any(p.match(suite) for p in _ETIQUETTES.values())
                    or re.match(r"^\s*(#{1,6}\s|(-{3,}|\*{3,}|_{3,})\s*$|```)", suite)):
                break
            morceaux.append(suite.strip())
        return " ".join(x for x in morceaux if x).strip().strip("*").strip()
    return ""


def _majuscule(texte):
    return texte[:1].upper() + texte[1:] if texte else texte


def _normaliser_variables(code):
    """Les modèles écrivent parfois {{VARIABLE}}, {VARIABLE} ou <VARIABLE> : tout devient [VARIABLE]."""
    code = _RE_ACCOLADES2.sub(lambda m: "[" + m.group(1).strip() + "]", code)
    code = _RE_ACCOLADES1.sub(lambda m: "[" + m.group(1).strip() + "]", code)
    return _RE_ANGLES.sub(lambda m: "[" + m.group(1).strip() + "]", code)


_ETIQUETTES_PROMPT = {
    "ROLE": "RÔLE", "CONTEXTE": "CONTEXTE", "TACHE": "TÂCHE", "CONTRAINTES": "CONTRAINTES",
    "CONTRAINTE": "CONTRAINTES", "FORMAT DE SORTIE": "FORMAT DE SORTIE", "FORMAT": "FORMAT DE SORTIE",
}
_RE_LIGNE_ETIQUETTE = re.compile(r"^(\s*)([A-Za-zÀ-ÿ' ]{3,20}?)\s*:\s*(.*)$")
CLAUSE_QUESTIONS = "Si une information essentielle manque, pose-moi jusqu\u2019à 3 questions avant de répondre."


def _corriger_etiquettes(code):
    """Remet en forme RÔLE / CONTEXTE / TÂCHE / CONTRAINTES / FORMAT DE SORTIE, même quand le modèle
    fait une faute (« CONTEXSE ») ou oublie un accent (« TACHE »). Les autres lignes ne sont pas touchées."""
    sortie = []
    for ligne in code.split("\n"):
        m = _RE_LIGNE_ETIQUETTE.match(ligne)
        if m:
            brut = _sans_accents(m.group(2)).upper().strip()
            cle = brut if brut in _ETIQUETTES_PROMPT else None
            if cle is None:
                proches = difflib.get_close_matches(brut, list(_ETIQUETTES_PROMPT), n=1, cutoff=0.8)
                cle = proches[0] if proches else None
            if cle:
                ligne = f"{m.group(1)}{_ETIQUETTES_PROMPT[cle]} : {m.group(3)}"
        sortie.append(ligne)
    return "\n".join(sortie)


def _ajouter_clause_questions(code):
    """Garantit que chaque prompt demande à l'IA de poser des questions si une information manque
    (les modèles l'oublient souvent) : la phrase est ajoutée à la ligne CONTRAINTES."""
    if re.search(r"\b(?:pose|demande)[- ]moi\b", code, re.I):      # « pose-moi ... », « demande-moi ... »
        return code
    lignes = code.split("\n")
    for k in range(len(lignes) - 1, -1, -1):
        if re.match(r"^\s*CONTRAINTES\s*:", lignes[k]):
            fin = lignes[k].rstrip()
            lignes[k] = fin + ("" if fin.endswith((".", "!", "?", "\u00bb", ")")) else ".") + " " + CLAUSE_QUESTIONS
            return "\n".join(lignes)
    return code.rstrip() + "\n" + CLAUSE_QUESTIONS


def _mesurer_prompt(code):
    """(nombre de mots, nombre de variables [..], nombre d'étiquettes RÔLE/CONTEXTE/TÂCHE...)"""
    sans_accent = _sans_accents(code).upper()
    champs = sum(1 for c in _CHAMPS_PROMPT if re.search(rf"\b{c}\b\s*\**\s*:", sans_accent))
    return len(code.split()), len(_RE_VARIABLE.findall(code)), champs


def _titre_propre(brut, maxi=90):
    t = re.sub(r"^#+\s*", "", brut).strip().strip("*").strip()
    t = re.sub(r"^\d{1,2}\s*[.)\-:]\s*", "", t)
    t = t.strip(" .:;*")
    if len(t) < 3:
        raise ErreurModele("titre de prompt vide (une ligne « ### Titre » est attendue)")
    if len(t) > maxi:
        t = t[:maxi].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return t


def _nom_categorie(ligne):
    t = re.sub(r"^#+\s*", "", ligne).strip().strip("*").strip()
    t = re.sub(r"^(partie|cat[ée]gorie)\s*\d+\s*[:.\-\u2013\u2014]\s*", "", t, flags=re.I)
    t = t.strip(" .:;*")
    return t if 3 <= len(t) <= 80 else None


def analyser_partie_pack(texte, categorie_defaut="Prompts"):
    """Vérifie et met en forme une partie du pack.
    Renvoie ((catégorie, [5 prompts]), avertissements) ou lève ErreurModele (structure inutilisable).
    Les messages ne contiennent que des chiffres et des consignes : jamais de contenu (logs publics)."""
    lignes = nettoyer_markdown(texte).split("\n")
    blocs, avant, courant, dans_code = [], [], None, False
    for ligne in lignes:
        if ligne.strip().startswith("```"):
            dans_code = not dans_code
        if not dans_code and re.match(r"^###\s+\S", ligne):
            courant = {"titre": ligne, "lignes": []}
            blocs.append(courant)
        elif courant is not None:
            courant["lignes"].append(ligne)
        else:
            avant.append(ligne)
    if len(blocs) < NB_PROMPTS_PAR_PARTIE:
        raise ErreurModele(f"{len(blocs)} prompts trouvés au lieu de {NB_PROMPTS_PAR_PARTIE} "
                           "(chaque prompt commence par une ligne « ### Titre »)")

    avert = []
    categorie = next((_nom_categorie(l) for l in avant if re.match(r"^#{1,2}\s+\S", l)), None)
    if not categorie:
        categorie = categorie_defaut
        avert.append("titre de catégorie manquant (une ligne « ## Nom de la catégorie » est attendue)")

    prompts, stats = [], []
    for k, bloc in enumerate(blocs[:NB_PROMPTS_PAR_PARTIE], start=1):
        titre = _titre_propre(bloc["titre"])
        fences = [i for i, l in enumerate(bloc["lignes"]) if l.strip().startswith("```")]
        if len(fences) < 2:
            raise ErreurModele(f"le prompt n°{k} n'est pas placé dans un bloc de code (``` ... ```)")
        code = "\n".join(bloc["lignes"][fences[0] + 1:fences[1]]).strip("\n")
        code = _normaliser_variables(code).replace("**", "")
        code = _ajouter_clause_questions(_corriger_etiquettes(code))
        mots, variables, champs = _mesurer_prompt(code)
        if mots < 40:
            raise ErreurModele(f"le prompt n°{k} est trop court ({mots} mots ; 120 à 180 attendus)")
        if mots > 600:
            raise ErreurModele(f"le prompt n°{k} est trop long ({mots} mots ; 120 à 180 attendus)")
        if _semble_anglais(code):
            raise ErreurModele(f"le prompt n°{k} est rédigé en anglais (le français est demandé)")
        hors_code = bloc["lignes"][:fences[0]] + bloc["lignes"][fences[1] + 1:]
        quand, resultat, astuce = (_majuscule(_champ(hors_code, c)) for c in ("quand", "resultat", "astuce"))
        prompts.append({"titre": titre, "quand": quand, "prompt": code,
                        "resultat": resultat, "astuce": astuce})
        stats.append((mots, variables, champs))

    if any(v < 2 for _, v, _ in stats):
        avert.append("nombre de variables [EN MAJUSCULES] par prompt : "
                     + ",".join(str(v) for _, v, _ in stats) + " (au moins 2 par prompt sont attendues)")
    sans_schema = sum(1 for _, _, c in stats if c < 3)
    if sans_schema:
        avert.append(f"{sans_schema} prompt(s) sans le schéma RÔLE / CONTEXTE / TÂCHE / CONTRAINTES / FORMAT DE SORTIE")
    manquantes = sum(1 for p in prompts for cle in ("quand", "resultat", "astuce") if not p[cle])
    if manquantes:
        avert.append(f"{manquantes} ligne(s) manquante(s) parmi « Quand l'utiliser », « Résultat attendu » et « Astuce »")
    courts = sum(1 for m, _, _ in stats if m < 60)
    if courts:
        avert.append(f"{courts} prompt(s) de moins de 60 mots (120 à 180 attendus)")
    return (categorie, prompts), avert


def assembler_pack(parties):
    """Assemble le produit final : mode d'emploi fixe + 3 parties numérotées + mentions."""
    morceaux = [MODE_EMPLOI_PACK]
    numero = 0
    for k, (categorie, prompts) in enumerate(parties, start=1):
        morceaux.append(f"## Partie {k} \u2014 {categorie}")
        for p in prompts:
            numero += 1
            carte = [f"### {numero}. {p['titre']}"]
            if p.get("quand"):
                carte.append(f"**Quand l\u2019utiliser :** {p['quand']}")
            carte.append(f"```\n{p['prompt']}\n```")
            if p.get("resultat"):
                carte.append(f"**Résultat attendu :** {p['resultat']}")
            if p.get("astuce"):
                carte.append(f"**Astuce :** {p['astuce']}")
            morceaux.append("\n\n".join(carte))
    morceaux.append(MENTIONS_PACK)
    return "\n\n".join(morceaux)


# --- CV et template Notion : contrôle d'un document en un seul bloc ---
def analyser_document(type_produit, texte):
    """Renvoie (document, avertissements) ou lève ErreurModele si le document est inutilisable."""
    exig = EXIGENCES_DOCUMENT[type_produit]
    t = nettoyer_markdown(texte)
    m = re.search(r"(?m)^##\s+\S", t)
    if not m:
        raise ErreurModele("aucun titre de section « ## » (le document doit être découpé en sections « ## »)")
    t = t[m.start():]
    if _semble_anglais(t):
        raise ErreurModele("document rédigé en anglais (le français est demandé)")
    if len(t) < exig["minimum"]:
        raise ErreurModele(f"document beaucoup trop court ({len(t)} caractères)")
    avert = []
    nb = len(re.findall(r"(?m)^##\s+\S", t))
    if nb < exig["sections"]:
        avert.append(f"{nb} sections « ## » au lieu de {exig['sections']} attendues")
    if len(t) < exig["caracteres"]:
        avert.append(f"document plus court que prévu ({len(t)} caractères, {exig['caracteres']} attendus)")
    return t, avert


def pour_airtable(markdown):
    """Version lisible dans Airtable : chaque prompt (bloc ```) devient une citation « > »,
    et les cases à cocher prennent la forme « [ ] » que l'API Airtable utilise."""
    sortie, code, dans_code = [], [], False
    for ligne in markdown.split("\n"):
        if ligne.strip().startswith("```"):
            if dans_code:
                utiles = [l.strip() for l in code if l.strip()]
                for k, l in enumerate(utiles):
                    sortie.append("> " + l)
                    if k < len(utiles) - 1:
                        sortie.append(">")
                dans_code, code = False, []
            else:
                dans_code = True
            continue
        if dans_code:
            code.append(ligne)
        else:
            sortie.append(re.sub(r"^(\s*)[-*]\s+\[( |x|X)\]\s+", r"\1[\2] ", ligne))
    if dans_code:                 # bloc jamais refermé : on garde le texte tel quel
        sortie += code
    return "\n".join(sortie)


# ======================================================================
# GÉNÉRATION (modèle de secours automatique)
# ======================================================================
class _Moteur:
    """Choisit le modèle Groq. Un contenu refusé ou imparfait est retenté avec des consignes précises ;
    ensuite on garde la meilleure version, ou on passe au modèle suivant."""

    def __init__(self, modeles):
        self.modeles = modeles
        self.rang = 0

    @staticmethod
    def _consigne(raison):
        return ("\n\nATTENTION : ta réponse précédente ne respectait pas le format demandé "
                f"({raison}). Recommence en corrigeant ce point, sans rien changer aux autres consignes.")

    def generer(self, systeme, prompt, max_tokens, analyseur, etiquette):
        while self.rang < len(self.modeles):
            modele = self.modeles[self.rang]
            meilleur, consigne = None, ""
            for essai in range(1, ESSAIS_PAR_MODELE + 1):
                suffixe = f" (essai {essai})" if essai > 1 else ""
                print(f"  {etiquette} - modèle : {modele}{suffixe}")
                try:
                    texte = appeler_groq(modele, systeme, prompt + consigne, max_tokens)
                except ErreurModele as e:
                    print(f"  -> {modele} n'a pas pu répondre : {e}")
                    break                    # quota ou panne : inutile de réessayer ce modèle
                try:
                    valeur, avertissements = analyseur(texte)
                except ErreurModele as e:
                    print(f"  -> réponse de {modele} refusée : {e}")
                    consigne = self._consigne(str(e))
                    continue
                if not avertissements:
                    return valeur
                bilan = " ; ".join(avertissements)
                print(f"  -> réponse utilisable mais imparfaite : {bilan}")
                if meilleur is None or len(avertissements) < len(meilleur[1]):
                    meilleur = (valeur, avertissements)
                consigne = self._consigne(bilan)
            if meilleur is not None:
                print("  -> meilleure version retenue malgré ces réserves : à relire avec attention avant de valider.")
                return meilleur[0]
            self.rang += 1
        raise ErreurGeneration(f"aucun modèle n'a pu produire ({etiquette})")


def generer_pack(titre, moteur):
    """Pack de 15 prompts en 3 appels de 5 prompts. Renvoie (markdown, nombre de prompts)."""
    parties, deja = [], []
    for numero, (axe_nom, axe_desc) in enumerate(AXES_PACK, start=1):
        prompt = UTILISATEUR_PACK.format(
            titre=titre, numero=numero, total=NB_PARTIES_PACK,
            axe_nom=axe_nom, axe_desc=axe_desc,
            deja=" ; ".join(deja) if deja else "aucun")
        etiquette = f"Partie {numero}/{NB_PARTIES_PACK}"
        defaut = axe_nom.split(":", 1)[-1].strip().capitalize()
        categorie, prompts = moteur.generer(
            SYSTEME_PACK, prompt, MAX_TOKENS_PARTIE,
            lambda t, d=defaut: analyser_partie_pack(t, d), etiquette)
        parties.append((categorie, prompts))
        deja += [p["titre"] for p in prompts]
        print(f"  {etiquette} validée ({len(prompts)} prompts)")
    return assembler_pack(parties), sum(len(p) for _, p in parties)


def generer_document(type_produit, titre, moteur):
    cfg = EXIGENCES_DOCUMENT[type_produit]
    corps = moteur.generer(
        cfg["systeme"], cfg["utilisateur"].format(titre=titre), MAX_TOKENS_DOCUMENT,
        lambda t: analyser_document(type_produit, t), "Document")
    return cfg["banniere"] + "\n\n" + corps


# ======================================================================
# PDF (best effort : un souci de PDF ne bloque jamais le contenu)
# ======================================================================
def _nom_fichier_pdf(titre):
    ascii_ = unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:60].strip("-")
    return (slug or "produit") + ".pdf"


def _etat_pdf():
    """Dit si le module PDF est utilisable, sans rien afficher d'autre."""
    try:
        import mise_en_page
        import reportlab
        return f"prêt (reportlab {reportlab.Version})"
    except Exception as e:
        return (f"indisponible ({type(e).__name__}) : vérifie que mise_en_page.py est dans le dépôt "
                "et que requirements.txt contient reportlab")


def fabriquer_pdf(type_produit, titre, markdown, nb_prompts=None):
    """Renvoie les octets du PDF, ou None (avec un avertissement SANS contenu) si impossible."""
    try:
        import mise_en_page
    except Exception as e:
        print(f"AVERTISSEMENT : PDF non généré, module ou bibliothèque absent ({type(e).__name__}). "
              "Vérifie que mise_en_page.py est dans le dépôt et que requirements.txt contient reportlab.")
        return None

    pres = PRESENTATION_PDF.get(type_produit, {})
    sous_titre = pres.get("sous_titre", "")
    if type_produit == "prompt_pack" and nb_prompts:
        sous_titre = f"{nb_prompts} prompts IA prêts à copier-coller pour Assistants Virtuels et OBM"
    try:
        pdf = mise_en_page.construire_pdf(
            titre, markdown, sous_titre=sous_titre, marque=MARQUE,
            etiquette=pres.get("etiquette", ""), legende_code=pres.get("legende_code", ""),
            saut_avant_partie=pres.get("saut_avant_partie", False))
    except Exception as e:
        print(f"AVERTISSEMENT : PDF non généré ({type(e).__name__}). Le contenu texte est conservé.")
        return None
    if not pdf.startswith(b"%PDF"):
        print("AVERTISSEMENT : PDF non généré (fichier invalide).")
        return None
    if len(pdf) > TAILLE_MAX_PDF_OCTETS:
        print("AVERTISSEMENT : PDF trop lourd pour Airtable (plus de 4,5 Mo), non joint.")
        return None
    return pdf


# ======================================================================
# AIRTABLE
# ======================================================================
def _erreur_airtable(e):
    """Résumé d'erreur Airtable SANS url ni identifiant (logs publics)."""
    r = getattr(e, "response", None)
    if r is None:
        return f"problème réseau ({type(e).__name__})"
    try:
        err = r.json().get("error", "inconnu")
        type_err = err.get("type", "inconnu") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        type_err = "inconnu"
    indices = {
        401: "token invalide ou expiré : vérifie AIRTABLE_TOKEN",
        403: "le token n'a pas accès à cette base : Builder Hub > ton token > Access",
        404: "base introuvable ou table introuvable : vérifie AIRTABLE_BASE_ID "
             "(17 caractères, 'app...') et le nom exact de la table",
        422: "champ ou option manquant dans Airtable (Titre, Type, Statut, Contenu)",
    }
    indice = indices.get(r.status_code)
    suite = f" -> {indice}" if indice else ""
    return f"HTTP {r.status_code} ({type_err}){suite}"


def _erreur_joindre_pdf(e):
    r = getattr(e, "response", None)
    if r is not None and r.status_code in (404, 422):
        return (f"HTTP {r.status_code} -> champ '{CHAMP_PDF}' introuvable ou pas de type Attachment : "
                "crée ce champ dans la table Produits (type Attachment), puis relance")
    return _erreur_airtable(e)


def recuperer_prochaine_tache():
    """1er enregistrement avec Statut = 'à faire' (1 appel API)."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(AIRTABLE_TABLE, safe='')}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    params = {"filterByFormula": "{Statut} = 'à faire'", "maxRecords": 1}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    records = r.json().get("records", [])
    return records[0] if records else None


def marquer_a_valider(record_id, contenu):
    """Ajoute le contenu et passe le statut à 'à valider' (1 appel API)."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(AIRTABLE_TABLE, safe='')}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"fields": {"Contenu": contenu, "Statut": "à valider"}}
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def joindre_pdf(record_id, pdf, nom_fichier):
    """Ajoute le PDF au champ Attachment (envoi direct en base64, 5 Mo maximum)."""
    url = (f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/"
           f"{quote(CHAMP_PDF, safe='')}/uploadAttachment")
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "contentType": "application/pdf",
        "filename": nom_fichier,
        "file": base64.b64encode(pdf).decode("ascii"),
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()


def verifier_formats():
    """Avertissements sur des formats suspects. N'affiche JAMAIS les valeurs."""
    avis = []
    if not re.fullmatch(r"app[A-Za-z0-9]{14}", AIRTABLE_BASE_ID):
        avis.append(
            f"AIRTABLE_BASE_ID semble incorrect (longueur {len(AIRTABLE_BASE_ID)}, "
            "attendu 17 : 'app' + 14 caractères, sans espace ni '/')")
    if not AIRTABLE_TOKEN.startswith("pat"):
        avis.append("AIRTABLE_TOKEN ne commence pas par 'pat'")
    if not GROQ_API_KEY.startswith("gsk_"):
        avis.append("GROQ_API_KEY ne commence pas par 'gsk_'")
    return avis


# ======================================================================
# EXÉCUTION
# ======================================================================
def executer():
    print(f"Agent Créateur v{VERSION} | PDF : {_etat_pdf()}")
    manquants = [
        nom for nom, val in [
            ("GROQ_API_KEY", GROQ_API_KEY),
            ("AIRTABLE_TOKEN", AIRTABLE_TOKEN),
            ("AIRTABLE_BASE_ID", AIRTABLE_BASE_ID),
        ] if not val
    ]
    if manquants:
        print(f"ERREUR : secret(s) manquant(s) -> {', '.join(manquants)}")
        print("Vérifie Settings > Secrets and variables > Actions sur GitHub.")
        sys.exit(1)

    for avis in verifier_formats():
        print(f"AVERTISSEMENT : {avis}")
    print(f"Table utilisée : {AIRTABLE_TABLE}")

    print("Recherche d'une tâche 'à faire' dans Airtable...")
    try:
        tache = recuperer_prochaine_tache()
    except requests.exceptions.RequestException as e:
        print(f"ERREUR Airtable (lecture) : {_erreur_airtable(e)}")
        sys.exit(1)

    if not tache:
        print("Rien à faire : aucune ligne avec Statut = 'à faire'.")
        return

    champs = tache.get("fields", {})
    titre = str(champs.get("Titre", "Sans titre"))[:200]
    type_produit = champs.get("Type")

    if type_produit not in TYPES_PRODUITS:
        print("ERREUR : le champ Type est vide ou inconnu.")
        print(f"Types valides : {list(TYPES_PRODUITS)}")
        sys.exit(1)

    print(f"Génération en cours pour un produit de type : {type_produit}")
    moteur = _Moteur(liste_modeles())
    nb_prompts = None
    try:
        if type_produit == "prompt_pack":
            markdown, nb_prompts = generer_pack(titre, moteur)
        else:
            markdown = generer_document(type_produit, titre, moteur)
    except ErreurFatale as e:
        print(f"ERREUR : {e}")
        sys.exit(1)
    except ErreurGeneration as e:
        print(f"ERREUR : {e}. La tâche reste 'à faire' (rien n'a été écrit dans Airtable).")
        print("Vérifie console.groq.com/docs/rate-limits (limites et modèles disponibles).")
        sys.exit(1)

    pdf = fabriquer_pdf(type_produit, titre, markdown, nb_prompts)

    try:
        marquer_a_valider(tache["id"], pour_airtable(markdown))
    except requests.exceptions.RequestException as e:
        print(f"ERREUR Airtable (écriture) : {_erreur_airtable(e)}")
        sys.exit(1)
    print("Contenu enregistré : 1 produit est maintenant 'à valider' dans Airtable.")

    if pdf:
        try:
            joindre_pdf(tache["id"], pdf, _nom_fichier_pdf(titre))
            print(f"PDF joint au champ '{CHAMP_PDF}' ({len(pdf) // 1024} Ko).")
        except requests.exceptions.RequestException as e:
            print(f"AVERTISSEMENT : PDF non joint ({_erreur_joindre_pdf(e)}).")
            print("Le contenu texte est bien enregistré ; seul le PDF manque.")

    print("Terminé.")


def principal():
    """Point d'entrée. Une erreur imprévue n'affiche que son nom : jamais de trace brute
    dans les logs publics (elle pourrait contenir une adresse ou un identifiant)."""
    try:
        executer()
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERREUR inattendue : {type(e).__name__} (détails masqués : le dépôt est public).")
        sys.exit(1)


if __name__ == "__main__":
    principal()
