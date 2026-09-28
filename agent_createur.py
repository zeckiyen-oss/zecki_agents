#!/usr/bin/env python3
"""
AGENT CREATEUR (v2.1)
===================
Rôle : va chercher dans Airtable la prochaine tâche au statut "à faire",
génère le contenu avec Groq, puis remet la tâche à jour avec le statut
"à valider". Toi seul valides ou refuses ensuite dans Airtable.

CE QUE CET AGENT NE FAIT JAMAIS (sécurité) :
- Il ne publie rien sur Maketou/Payhip/Gumroad/Etsy.
- Il ne touche à aucun compte de paiement ou de retrait.
- Il ne traite qu'UNE tâche par exécution (protège tes quotas).

REPO PUBLIC : les logs GitHub sont lisibles par tout le monde.
Ce script n'y écrit donc jamais : clés, titres de produits, contenus,
adresses ou identifiants Airtable.

Toutes les clés viennent de variables d'environnement (Secrets GitHub).
"""

import os
import re
import sys
import time
from urllib.parse import quote

import requests

# --- Configuration (lue depuis GitHub, jamais en dur) ---
def _env(nom):
    """Lit une variable en retirant espaces et retours à la ligne invisibles
    (très fréquents après un copier-coller sur téléphone)."""
    return (os.environ.get(nom) or "").strip()


GROQ_API_KEY = _env("GROQ_API_KEY")
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = _env("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = _env("AIRTABLE_TABLE") or "Produits"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Modèles gratuits Groq (vérifiés sur console.groq.com/docs/rate-limits).
# Chaque modèle a son propre quota : si le 1er est saturé, on passe au 2e.
# Pour changer de modèle sans toucher au code : variable GitHub GROQ_MODELS
# (liste séparée par des virgules).
MODELES_PAR_DEFAUT = "openai/gpt-oss-120b,openai/gpt-oss-20b"

MAX_TOKENS_REPONSE = 5000     # réflexion + texte final
ATTENTE_MAX_SECONDES = 90     # au-delà, on ne patiente pas : modèle suivant
MAX_RETRIES_429 = 2
LONGUEUR_MIN_CONTENU = 200    # en dessous, on considère la réponse ratée

# --- Les 3 formats de produits validés pour la niche VA/OBM ---
PROMPTS = {
    "notion_template": """Tu es un expert en organisation pour Assistants Virtuels (VA) et Online Business Managers (OBM) indépendants.
Crée le contenu détaillé d'un template Notion sur le thème : "{titre}".
Public cible : VA/OBM qui gèrent plusieurs clients et veulent paraître pro.
Réponds en français, en Markdown, avec :
1. Nom du template + description courte orientée bénéfice client (2 phrases)
2. Liste des bases de données à créer, avec pour chacune la liste des propriétés/colonnes et leur type (texte, date, statut, formule...)
3. Les vues à configurer pour chaque base (ex : Kanban par statut, Calendrier par échéance)
4. 3 lignes d'exemple de données réalistes pour illustrer l'usage
5. Un texte d'introduction à mettre en haut de la page (ton chaleureux et professionnel)
Sois concret et immédiatement utilisable, évite les généralités.""",

    "prompt_pack": """Tu es un expert en prompt engineering pour Assistants Virtuels (VA) indépendants.
Crée un pack de 15 prompts IA prêts à l'emploi sur le thème : "{titre}".
Public cible : VA qui utilisent ChatGPT/Claude pour gagner du temps sur des tâches clients.
Réponds en français, en Markdown. Pour chaque prompt donne :
- Un titre court
- Le prompt complet, prêt à copier-coller, avec des [placeholders] clairs à remplacer
- Une phrase expliquant quand l'utiliser
Classe les 15 prompts en 3 catégories de 5, pertinentes pour le thème donné.""",

    "cv_template": """Tu es un expert en recrutement freelance pour Assistants Virtuels (VA) et OBM.
Crée le contenu d'un template de CV/profil professionnel sur le thème : "{titre}".
Public cible : VA/OBM qui cherchent à décrocher des clients (Upwork, LinkedIn, etc.).
Réponds en français, en Markdown, avec :
1. Structure complète du document (sections dans l'ordre, avec leur rôle)
2. Un exemple rédigé pour chaque section (texte réaliste et convaincant, à adapter)
3. 5 conseils courts pour se différencier de la concurrence
4. Une accroche de présentation (2-3 phrases) réutilisable sur les plateformes freelance""",
}


class ErreurModele(Exception):
    """Ce modèle ne peut pas répondre : on essaie le suivant."""


class ErreurFatale(Exception):
    """Erreur qui ne se règle pas en changeant de modèle : on arrête tout."""


def liste_modeles():
    brut = os.environ.get("GROQ_MODELS") or MODELES_PAR_DEFAUT
    return [m.strip() for m in brut.split(",") if m.strip()]


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


def appeler_groq(modele, prompt):
    """Appelle UN modèle Groq. Renvoie le texte, ou lève ErreurModele/ErreurFatale."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": modele,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_completion_tokens": MAX_TOKENS_REPONSE,
        "reasoning_effort": "low",     # peu de réflexion = plus de place pour le texte
        "include_reasoning": False,
    }

    for tentative in range(MAX_RETRIES_429 + 1):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
        except requests.exceptions.RequestException as e:
            raise ErreurModele(f"problème réseau ({type(e).__name__})")

        if r.status_code == 429:
            attente = _lire_retry_after(r)
            if attente > ATTENTE_MAX_SECONDES or tentative == MAX_RETRIES_429:
                raise ErreurModele("limite Groq atteinte (429)")
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


def executer():
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
    format_produit = champs.get("Type")

    if format_produit not in PROMPTS:
        print("ERREUR : le champ Type est vide ou inconnu.")
        print(f"Types valides : {list(PROMPTS.keys())}")
        sys.exit(1)

    print(f"Génération en cours pour un produit de type : {format_produit}")
    prompt = PROMPTS[format_produit].format(titre=titre)

    contenu = None
    for modele in liste_modeles():
        print(f"  Modèle essayé : {modele}")
        try:
            contenu = appeler_groq(modele, prompt)
            break
        except ErreurModele as e:
            print(f"  -> {modele} n'a pas pu répondre : {e}")
        except ErreurFatale as e:
            print(f"ERREUR : {e}")
            sys.exit(1)

    if contenu is None:
        print("ERREUR : aucun modèle Groq n'a pu répondre. La tâche reste 'à faire'.")
        print("Vérifie console.groq.com/docs/rate-limits (limites et modèles disponibles).")
        sys.exit(1)

    try:
        marquer_a_valider(tache["id"], contenu)
    except requests.exceptions.RequestException as e:
        print(f"ERREUR Airtable (écriture) : {_erreur_airtable(e)}")
        sys.exit(1)

    print("Terminé : 1 produit est maintenant 'à valider' dans Airtable.")


if __name__ == "__main__":
    executer()
