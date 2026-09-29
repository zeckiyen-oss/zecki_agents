#!/usr/bin/env python3
"""
MISE EN PAGE PDF (v1.0) - compagnon de agent_createur.py
=========================================================
Transforme le texte Markdown d'un produit en PDF propre :
couverture, sommaire cliquable, titres, listes, cartes de prompts,
variables [EN ORANGE], pied de page numéroté.

- Renvoie les octets du PDF : n'écrit rien sur le disque.
- N'affiche JAMAIS de contenu (repo public = logs publics).
- Dépendance : reportlab (voir requirements.txt).
- Polices standard du PDF (Helvetica) : aucun fichier de police à installer.
"""

import io
import re
import unicodedata
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Flowable, Frame, HRFlowable,
    NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

# --- Palette -----------------------------------------------------------
LARGEUR_PAGE, HAUTEUR_PAGE = A4
MARGE = 20 * mm
LARGEUR_UTILE = LARGEUR_PAGE - 2 * MARGE

MARINE = colors.HexColor("#0B1F3A")
TURQUOISE = colors.HexColor("#0E9F8E")
MENTHE = colors.HexColor("#E6F6F3")
ENCRE = colors.HexColor("#1F2937")
GRIS = colors.HexColor("#64748B")
FOND = colors.HexColor("#F4F7FB")
BORDURE = colors.HexColor("#D5DDE8")
TURQUOISE_FONCE = "#0B7A6E"     # texte des étiquettes sur fond blanc
ORANGE = "#C2410C"              # variables à remplacer
CLAIR = "#A9C3DA"               # texte discret sur fond marine

# --- Nettoyage des caractères pour les polices standard du PDF ---------
_REMPLACEMENTS = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-",
    "\u2192": "->", "\u2190": "<-", "\u21d2": "=>", "\u2194": "<->",
    "\u2265": ">=", "\u2264": "<=", "\u2260": "!=", "\u2248": "~",
    "\u2713": "v", "\u2714": "v", "\u2717": "x", "\u2718": "x",
    "\u25cf": "\u2022", "\u25aa": "\u2022", "\u25e6": "o", "\u2023": "\u2022",
    "\u25b6": ">", "\u25ba": ">",
    "\u202f": "\u00a0", "\u2009": " ", "\u2002": " ", "\u2003": " ", "\u200a": " ",
    "\u2028": "\n", "\u2029": "\n", "\u00ad": "",
    "\u2032": "'", "\u2033": '"',
}


def _vers_cp1252(texte):
    """Garde ce que les polices standard savent écrire (accents français,
    guillemets, tirets, euro...) ; remplace ou retire le reste (emoji, flèches)."""
    sortie = []
    for c in texte:
        for d in _REMPLACEMENTS.get(c, c):
            try:
                d.encode("cp1252")
                sortie.append(d)
            except UnicodeEncodeError:
                base = "".join(x for x in unicodedata.normalize("NFKD", d)
                               if not unicodedata.combining(x))
                try:
                    base.encode("cp1252")
                    sortie.append(base)
                except UnicodeEncodeError:
                    pass    # emoji ou symbole exotique : ignoré
    return "".join(sortie)


# --- Markdown en ligne -> mini-HTML de reportlab -----------------------
_RE_CODE = re.compile(r"`([^`\n]+)`")
_RE_GRAS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_RE_ITAL_ETOILE = re.compile(r"(?<![\*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\*\w])")
_RE_ITAL_TIRET = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_RE_BARRE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_RE_VARIABLE = re.compile(r"\[([^\[\]\n]{1,70})\](?!\()")


def _variables(html):
    return _RE_VARIABLE.sub(
        lambda m: f'<font color="{ORANGE}"><b>[{m.group(1)}]</b></font>', html)


def _inline(texte):
    t = _vers_cp1252(texte)
    codes = []

    def garder_code(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = _RE_CODE.sub(garder_code, t)
    t = escape(t)
    t = _RE_GRAS.sub(r"<b>\1</b>", t)
    t = _RE_ITAL_ETOILE.sub(r"<i>\1</i>", t)
    t = _RE_ITAL_TIRET.sub(r"<i>\1</i>", t)
    t = _RE_BARRE.sub(r"<strike>\1</strike>", t)
    t = t.replace("**", "")
    t = _variables(t)
    t = re.sub(
        r"\x00(\d+)\x00",
        lambda m: '<font face="Courier" color="#0B1F3A">%s</font>' % escape(codes[int(m.group(1))]),
        t)
    return t


def _sans_balises(html):
    return re.sub(r"<[^>]+>", "", html)


def _para(html, style, **kw):
    """Paragraph qui ne plante jamais : si le balisage est invalide, repli en texte brut."""
    try:
        return Paragraph(html, style, **kw)
    except Exception:
        return Paragraph(escape(_sans_balises(html)), style, **kw)


_RE_ETIQUETTE_CODE = re.compile(r"^([A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý0-9 '’/&()\-]{1,34}?)\s*:\s*(.*)$")


def _html_code(texte):
    """Bloc de prompt : étiquettes RÔLE/CONTEXTE... en turquoise, variables en orange."""
    sortie = []
    for ligne in _vers_cp1252(texte).split("\n"):
        ligne = ligne.rstrip()
        if not ligne.strip():
            sortie.append("&nbsp;")
            continue
        indent = len(ligne) - len(ligne.lstrip(" "))
        corps = ligne.lstrip(" ")
        m = _RE_ETIQUETTE_CODE.match(corps)
        if m:
            html = (f'<font color="{TURQUOISE_FONCE}"><b>{escape(m.group(1))} :</b></font> '
                    + _variables(escape(m.group(2))))
        else:
            html = _variables(escape(corps))
        sortie.append("&nbsp;" * indent + html)
    return "<br/>".join(sortie)


# --- Analyse du Markdown ------------------------------------------------
_RE_TITRE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RE_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_RE_PUCE = re.compile(r"^(\s*)([-*+\u2022])\s+(.*)$")
_RE_NUM = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_RE_CASE = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_RE_ETIQ_DEBUT = re.compile(r"^\*\*[^*]{2,60}\*\*\s*:|^\*\*[^*]{2,60}:\*\*")


def _debut_de_bloc(ligne):
    t = ligne.strip()
    return (not t or t.startswith(("```", "~~~", ">")) or _RE_TITRE.match(t)
            or _RE_HR.match(t) or _RE_PUCE.match(ligne) or _RE_NUM.match(ligne)
            or _RE_ETIQ_DEBUT.match(t))


def _lire_liste(lignes, i, blocs):
    n = len(lignes)
    ordonnee = bool(_RE_NUM.match(lignes[i]))
    debut = int(_RE_NUM.match(lignes[i]).group(2)) if ordonnee else 1
    items = []
    while i < n:
        ligne = lignes[i]
        mp, mn = _RE_PUCE.match(ligne), _RE_NUM.match(ligne)
        if not (mp or mn):
            t = ligne.strip()
            if (t and ligne.startswith((" ", "\t")) and items
                    and not _RE_TITRE.match(t) and not t.startswith(("```", ">"))):
                cible = items[-1]["enfants"][-1] if items[-1]["enfants"] else items[-1]
                cible["texte"] += " " + t
                i += 1
                continue
            break
        m = mp or mn
        indent = len(m.group(1).expandtabs(4))
        if indent < 2 and items and bool(mn) != ordonnee:
            break
        texte = m.group(3).strip()
        case = None
        mc = _RE_CASE.match(texte)
        if mc:
            case, texte = (mc.group(1) != " "), mc.group(2).strip()
        item = {"texte": texte, "case": case, "enfants": []}
        if indent >= 2 and items:
            items[-1]["enfants"].append(item)
        else:
            items.append(item)
        i += 1
    blocs.append(("liste", ordonnee, debut, items))
    return i


def _analyser(markdown):
    lignes = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocs, i, n = [], 0, len(lignes)
    while i < n:
        ligne = lignes[i]
        s = ligne.strip()
        if not s:
            i += 1
            continue
        if s.startswith(("```", "~~~")):
            cloture = s[:3]
            i += 1
            code = []
            while i < n and not lignes[i].strip().startswith(cloture):
                code.append(lignes[i])
                i += 1
            i += 1
            blocs.append(("code", "\n".join(code).strip("\n")))
            continue
        m = _RE_TITRE.match(s)
        if m:
            blocs.append(("titre", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if _RE_HR.match(s):
            blocs.append(("hr",))
            i += 1
            continue
        if s.startswith(">"):
            citation = []
            while i < n and lignes[i].strip().startswith(">"):
                citation.append(lignes[i].strip().lstrip(">").strip())
                i += 1
            blocs.append(("citation", citation))
            continue
        if _RE_PUCE.match(ligne) or _RE_NUM.match(ligne):
            i = _lire_liste(lignes, i, blocs)
            continue
        para = [s]
        i += 1
        while i < n and not _debut_de_bloc(lignes[i]):
            para.append(lignes[i].strip())
            i += 1
        blocs.append(("para", " ".join(para)))
    return blocs


# --- Styles ---------------------------------------------------------------
def _styles():
    base = dict(fontName="Helvetica", fontSize=10, leading=14.6, textColor=ENCRE)
    S = {}
    S["corps"] = ParagraphStyle("corps", spaceAfter=7, **base)
    S["puce"] = ParagraphStyle(
        "puce", leftIndent=16, bulletIndent=3, bulletFontName="Helvetica-Bold",
        bulletFontSize=10, spaceAfter=3, **base)
    S["puce2"] = ParagraphStyle(
        "puce2", parent=S["puce"], leftIndent=32, bulletIndent=19,
        fontSize=9.6, leading=13.8)
    S["case"] = ParagraphStyle("case", spaceAfter=0, **base)
    S["h3"] = ParagraphStyle(
        "h3", fontName="Helvetica-Bold", fontSize=13.5, leading=17,
        textColor=MARINE, spaceBefore=4, spaceAfter=3)
    S["h4"] = ParagraphStyle(
        "h4", fontName="Helvetica-Bold", fontSize=10.5, leading=14,
        textColor=colors.HexColor(TURQUOISE_FONCE), spaceBefore=8, spaceAfter=3)
    S["bande_sur"] = ParagraphStyle(
        "bande_sur", fontName="Helvetica-Bold", fontSize=8.5, leading=11,
        textColor=colors.HexColor("#7FE0D2"))
    S["bande_titre"] = ParagraphStyle(
        "bande_titre", fontName="Helvetica-Bold", fontSize=17, leading=21,
        textColor=colors.white)
    S["code"] = ParagraphStyle(
        "code", fontName="Helvetica", fontSize=9.2, leading=13.4, textColor=ENCRE,
        backColor=FOND, borderColor=BORDURE, borderWidth=0.7, borderPadding=8,
        borderRadius=3, leftIndent=8, rightIndent=8, spaceBefore=3, spaceAfter=12)
    S["legende"] = ParagraphStyle(
        "legende", fontName="Helvetica-Bold", fontSize=7.5, leading=10,
        textColor=colors.HexColor(TURQUOISE_FONCE), spaceBefore=6, spaceAfter=8)
    S["citation"] = ParagraphStyle(
        "citation", fontName="Helvetica-Oblique", fontSize=9.8, leading=14.2,
        textColor=ENCRE, spaceAfter=2)
    S["sommaire_titre"] = ParagraphStyle(
        "sommaire_titre", fontName="Helvetica-Bold", fontSize=22, leading=26,
        textColor=MARINE, spaceAfter=14)
    S["toc0"] = ParagraphStyle(
        "toc0", fontName="Helvetica-Bold", fontSize=10.5, leading=15,
        textColor=MARINE, spaceBefore=7, leftIndent=0)
    S["toc1"] = ParagraphStyle(
        "toc1", fontName="Helvetica", fontSize=9.5, leading=13.5,
        textColor=ENCRE, leftIndent=12)
    S["couv_sur"] = ParagraphStyle(
        "couv_sur", fontName="Helvetica-Bold", fontSize=10, leading=14,
        textColor=colors.HexColor("#7FE0D2"))
    S["couv_sous"] = ParagraphStyle(
        "couv_sous", fontName="Helvetica", fontSize=13, leading=19,
        textColor=colors.HexColor("#D6E4F0"))
    return S


def _style_titre_couverture(titre):
    n = len(titre)
    taille = 34 if n <= 30 else 28 if n <= 60 else 22
    return ParagraphStyle(
        "couv_titre", fontName="Helvetica-Bold", fontSize=taille,
        leading=taille * 1.2, textColor=colors.white)


# --- Éléments spéciaux ------------------------------------------------------
class _Ancre(Flowable):
    """Repère invisible : sert au sommaire cliquable et aux signets du PDF."""

    def __init__(self, niveau, texte, cle):
        Flowable.__init__(self)
        self.niveau, self.texte, self.cle = niveau, texte, cle
        self.width = self.height = 0

    def wrap(self, largeur, hauteur):
        return (0, 0)

    def draw(self):
        pass


class _ItemCase(Flowable):
    """Ligne de checklist : petite case dessinée (vide ou cochée) + texte."""

    DECALAGE = 18
    MARGE_BAS = 3

    def __init__(self, paragraphe, cochee=False):
        Flowable.__init__(self)
        self.p, self.cochee = paragraphe, cochee

    def wrap(self, largeur, hauteur):
        _, h = self.p.wrap(largeur - self.DECALAGE, hauteur)
        self.width, self.height = largeur, h + self.MARGE_BAS
        return self.width, self.height

    def draw(self):
        c = self.canv
        self.p.drawOn(c, self.DECALAGE, self.MARGE_BAS)
        interligne = self.p.style.leading
        centre = self.height - interligne / 2 - 0.5
        x, y, t = 3, centre - 4, 8.5
        c.saveState()
        c.setLineWidth(0.9)
        c.setStrokeColor(TURQUOISE)
        if self.cochee:
            c.setFillColor(TURQUOISE)
            c.roundRect(x, y, t, t, 1.5, stroke=1, fill=1)
            c.setStrokeColor(colors.white)
            c.setLineWidth(1.3)
            c.line(x + 1.9, y + 4.3, x + 3.6, y + 2.3)
            c.line(x + 3.6, y + 2.3, x + 6.8, y + 6.5)
        else:
            c.roundRect(x, y, t, t, 1.5, stroke=1, fill=0)
        c.restoreState()


class _Document(BaseDocTemplate):
    def beforeDocument(self):
        self._niveau_precedent = -1

    def afterFlowable(self, flowable):
        if not isinstance(flowable, _Ancre):
            return
        try:
            niveau = min(flowable.niveau, self._niveau_precedent + 1)
            self.canv.bookmarkPage(flowable.cle)
            self.canv.addOutlineEntry(flowable.texte, flowable.cle, niveau, closed=0)
            self._niveau_precedent = niveau
        except Exception:
            pass
        self.notify("TOCEntry", (min(flowable.niveau, 1), flowable.texte, self.page, flowable.cle))


def _bande(titre_html, sur_titre_html=None, styles=None):
    contenu = []
    if sur_titre_html:
        contenu.append(_para(sur_titre_html, styles["bande_sur"]))
        contenu.append(Spacer(1, 3))
    contenu.append(_para(titre_html, styles["bande_titre"]))
    t = Table([[contenu]], colWidths=[LARGEUR_UTILE])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), MARINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 13),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
        ("LINEBEFORE", (0, 0), (0, -1), 5, TURQUOISE),
    ]))
    t.spaceAfter = 12
    return t


def _encart(texte_lignes, styles):
    texte = " ".join(l for l in texte_lignes if l)
    html = _inline(texte)
    if len(texte) > 900:
        return _para(html, styles["citation"])
    t = Table([[_para(html, styles["citation"])]], colWidths=[LARGEUR_UTILE])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), MENTHE),
        ("LINEBEFORE", (0, 0), (0, -1), 3, TURQUOISE),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    t.spaceAfter = 8
    return t


def _html_paragraphe(texte):
    html = _inline(texte)
    # L'étiquette en début de paragraphe (« Quand l'utiliser : ») passe en turquoise
    return re.sub(r"^<b>(.*?)</b>",
                  rf'<font color="{TURQUOISE_FONCE}"><b>\1</b></font>', html, count=1)


def _html_titre3(texte):
    t = _vers_cp1252(texte)
    m = re.match(r"^(\d{1,3})[.)]\s+(.*)$", t)
    if m:
        return f'<font color="{TURQUOISE_FONCE}">{m.group(1)}.</font>&nbsp;&nbsp;{_inline(m.group(2))}'
    return _inline(t)


def _elements_liste(bloc, S):
    _, ordonnee, debut, items = bloc
    sortie = []
    numero = debut
    for item in items:
        if item["case"] is not None:
            sortie.append(_ItemCase(_para(_inline(item["texte"]), S["case"]),
                                    cochee=bool(item["case"])))
        elif ordonnee:
            sortie.append(_para(_inline(item["texte"]), S["puce"], bulletText=f"{numero}."))
        else:
            sortie.append(_para(_inline(item["texte"]), S["puce"], bulletText="\u2022"))
        numero += 1
        for enfant in item["enfants"]:
            sortie.append(_para(_inline(enfant["texte"]), S["puce2"], bulletText="-"))
    sortie.append(Spacer(1, 5))
    return sortie


def _eyebrow_partie(texte):
    m = re.match(r"^(Partie\s+\d+)\s*[\u2014\u2013-]\s*(.+)$", texte, flags=re.I)
    if m:
        return m.group(1).upper(), m.group(2)
    return None, texte


def _pousser_carte(story, carte):
    """Ajoute une carte (repère + titre + contenu). Si sa hauteur tient sur une page
    (moins de 110 mm), on saute de page plutôt que de la couper en deux."""
    ancre, elements = carte[0], carte[1:]
    hauteur = 0
    try:
        for f in elements:
            hauteur += f.wrap(LARGEUR_UTILE, 10000)[1] + f.getSpaceBefore() + f.getSpaceAfter()
    except Exception:
        hauteur = 0
    if 0 < hauteur <= 110 * mm:
        story.append(CondPageBreak(hauteur + 4 * mm))
    else:
        story.append(CondPageBreak(75 * mm))
    story.append(ancre)
    story.extend(elements)


# --- Point d'entrée ------------------------------------------------------------
def construire_pdf(titre, markdown, sous_titre="", marque="", etiquette="",
                   legende_code="", saut_avant_partie=False):
    """Renvoie les octets du PDF. Lève une exception en cas d'échec (l'appelant décide)."""
    S = _styles()
    titre_txt = _vers_cp1252(str(titre)).strip() or "Document"
    marque_txt = _vers_cp1252(str(marque)).strip()
    blocs = _analyser(markdown)
    if not blocs:
        raise ValueError("contenu vide")

    # 1er titre de niveau 1 identique au titre du produit : inutile de le répéter
    if blocs and blocs[0][0] == "titre" and blocs[0][1] == 1:
        blocs = blocs[1:]

    story = []
    # --- couverture
    story += [
        Spacer(1, 78 * mm),
        _para(escape(_vers_cp1252(etiquette).upper()) or "PRODUIT NUMERIQUE", S["couv_sur"]),
        Spacer(1, 5 * mm),
        _para(escape(titre_txt), _style_titre_couverture(titre_txt)),
        Spacer(1, 6 * mm),
        HRFlowable(width=42 * mm, thickness=3.5, color=TURQUOISE, hAlign="LEFT",
                   spaceBefore=0, spaceAfter=8),
    ]
    if sous_titre:
        story.append(_para(escape(_vers_cp1252(sous_titre)), S["couv_sous"]))
    story += [NextPageTemplate("corps"), PageBreak()]

    # --- sommaire (seulement s'il y a de quoi lister)
    nb_titres = sum(1 for b in blocs if b[0] == "titre" and b[1] in (2, 3))
    if nb_titres >= 3:
        toc = TableOfContents()
        toc.levelStyles = [S["toc0"], S["toc1"]]
        toc.dotsMinLevel = 0
        story += [_para("Sommaire", S["sommaire_titre"]), toc, PageBreak()]

    # --- corps
    # Chaque prompt (titre ###) forme une « carte » : si elle est courte, elle n'est jamais
    # coupée entre deux pages ; si elle est longue, elle se coupe normalement.
    compteur = 0
    deja_un_titre = False
    carte = None

    def ajouter(*elements):
        (carte if carte is not None else story).extend(elements)

    for bloc in blocs:
        genre = bloc[0]
        if genre == "titre":
            niveau, texte = bloc[1], bloc[2]
            if niveau <= 3 and carte is not None:
                _pousser_carte(story, carte)
                carte = None
            if niveau <= 2:
                compteur += 1
                cle = f"sec{compteur}"
                eyebrow, nom = _eyebrow_partie(texte)
                if saut_avant_partie and eyebrow and deja_un_titre:
                    story.append(PageBreak())
                else:
                    story.append(CondPageBreak(60 * mm))
                deja_un_titre = True
                story.append(_Ancre(0, _vers_cp1252(texte), cle))
                story.append(_bande(_inline(nom), escape(eyebrow) if eyebrow else None, S))
            elif niveau == 3:
                compteur += 1
                cle = f"sec{compteur}"
                carte = [_Ancre(1, _vers_cp1252(texte), cle),
                         _para(_html_titre3(texte), S["h3"]),
                         HRFlowable(width="100%", thickness=0.6, color=BORDURE,
                                    spaceBefore=1, spaceAfter=6)]
            else:
                ajouter(CondPageBreak(30 * mm), _para(_inline(texte), S["h4"]))
        elif genre == "para":
            ajouter(_para(_html_paragraphe(bloc[1]), S["corps"]))
        elif genre == "liste":
            ajouter(*_elements_liste(bloc, S))
        elif genre == "citation":
            ajouter(_encart(bloc[1], S))
        elif genre == "code":
            if legende_code:
                ajouter(_para(escape(_vers_cp1252(legende_code).upper()), S["legende"]))
            ajouter(_para(_html_code(bloc[1]) or "&nbsp;", S["code"]))
        elif genre == "hr":
            ajouter(HRFlowable(width="100%", thickness=0.6, color=BORDURE,
                               spaceBefore=4, spaceAfter=8))
    if carte is not None:
        _pousser_carte(story, carte)

    # --- gabarits de page
    pied_gauche = (marque_txt or titre_txt)[:80]

    def fond_couverture(canv, doc):
        canv.saveState()
        canv.setFillColor(MARINE)
        canv.rect(0, 0, LARGEUR_PAGE, HAUTEUR_PAGE, stroke=0, fill=1)
        canv.setFillColor(TURQUOISE)
        canv.setFillAlpha(0.18)
        canv.circle(LARGEUR_PAGE - 25 * mm, HAUTEUR_PAGE - 35 * mm, 72 * mm, stroke=0, fill=1)
        canv.setFillAlpha(0.10)
        canv.circle(28 * mm, 40 * mm, 58 * mm, stroke=0, fill=1)
        canv.setFillAlpha(1)
        canv.setFillColor(TURQUOISE)
        canv.rect(0, 0, LARGEUR_PAGE, 7 * mm, stroke=0, fill=1)
        if marque_txt:
            canv.setFillColor(colors.HexColor(CLAIR))
            canv.setFont("Helvetica-Bold", 10)
            canv.drawString(22 * mm, 20 * mm, marque_txt[:70])
        canv.restoreState()

    def pied_de_page(canv, doc):
        canv.saveState()
        canv.setStrokeColor(BORDURE)
        canv.setLineWidth(0.5)
        canv.line(MARGE, 16 * mm, LARGEUR_PAGE - MARGE, 16 * mm)
        canv.setFont("Helvetica", 8)
        canv.setFillColor(GRIS)
        canv.drawString(MARGE, 11 * mm, pied_gauche)
        canv.drawRightString(LARGEUR_PAGE - MARGE, 11 * mm, f"Page {doc.page}")
        canv.restoreState()

    cadre_couv = Frame(22 * mm, 30 * mm, LARGEUR_PAGE - 44 * mm, HAUTEUR_PAGE - 60 * mm,
                       id="couv", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    cadre_corps = Frame(MARGE, 22 * mm, LARGEUR_UTILE, HAUTEUR_PAGE - 22 * mm - 20 * mm,
                        id="corps", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    tampon = io.BytesIO()
    doc = _Document(
        tampon, pagesize=A4,
        pageTemplates=[
            PageTemplate(id="couverture", frames=[cadre_couv], onPage=fond_couverture),
            PageTemplate(id="corps", frames=[cadre_corps], onPage=pied_de_page),
        ],
        title=titre_txt, author=marque_txt, subject=_vers_cp1252(sous_titre),
        creator="Agent Createur",
    )
    doc.multiBuild(story)
    return tampon.getvalue()
