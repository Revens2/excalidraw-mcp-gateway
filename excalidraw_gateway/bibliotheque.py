"""Bibliotheque distante Excalidraw : persistance serveur des dessins IA et humains.

Racine physique : ``/srv/excalidraw/data/bibliotheque`` (durable, deja couverte
par ``ReadWritePaths`` de ``excalidraw-gateway.service``).
Racine logique presentee a l'utilisateur et a ChatGPT : ``Excalidraw``.

Securite : tous les chemins manipules sont RELATIFS a la racine, normalises et
contenus. Sont refuses : chemins absolus, ``..``, segments vides/``.``,
separateurs Windows, octet NUL, liens symboliques sur le parcours, et toute
resolution finale hors racine. Les documents doivent porter l'extension
``.excalidraw`` et contenir un JSON de document Excalidraw standard
(``{"type": "excalidraw", "elements": [...], ...}``), editable tel quel dans
l'application Excalidraw officielle.

Aucun secret ici : le jeton d'ecriture HTTP est lu depuis l'environnement par
``app.py``, jamais journalise ni versionne.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

# Racine logique presentee a l'utilisateur (dossiers MCP, URLs, textes).
RACINE_LOGIQUE = "Excalidraw"

# Extension obligatoire des documents persistants.
EXTENSION = ".excalidraw"

# Sous-dossier dedie aux creations IA : l'autosave de `create_view` (sans
# `enregistrer_sous`) y range ses fichiers, sans jamais toucher aux dossiers
# humains existants.
DOSSIER_IA = "ia"

# Garde-fous de taille (alignes sur le MCP : 5 Mo max par entree).
TAILLE_MAX_DOC_OCTETS = 5 * 1024 * 1024
LONGUEUR_MAX_SEGMENT = 128
LONGUEUR_MAX_CHEMIN = 512

# Segments de nom toleres : lettres (incl. accentuees), chiffres et ponctuation
# courante. Tout le reste (dont ``..`` traite separement) est refuse.
_SEGMENT_OK = re.compile(r"^[\w][\w .()+\-]*$", re.UNICODE)

# Pseudo-elements emis par l'outil upstream `create_view` (indication de cadrage
# camera : `{"type": "cameraUpdate", "width": ..., "height": ..., "x": ..., "y": ...}`),
# presents dans chaque checkpoint relu. Ce ne sont PAS des elements de scene
# Excalidraw : persistés tels quels, ils cassent le rendu et le cadrage dans
# l'editeur officiel (canvas vide, bouton "Scroll back to content" inoperant ;
# constate live 2026-09-17 sur les dessins generes par ChatGPT). Filtres a
# l'ecriture (jamais persistés) ; l'editeur re-filtre a la lecture pour les
# fichiers deja pollues (disque inchange, re-sauvegarde naturelle ensuite).
ELEMENTS_NON_SCENE = frozenset({"cameraUpdate"})


def nettoyer_elements(elements: list) -> list:
    """Retire les pseudo-elements non-scene (ex. `cameraUpdate` upstream)."""
    return [e for e in elements
            if isinstance(e, dict) and e.get("type") not in ELEMENTS_NON_SCENE]


class ErreurBibliotheque(ValueError):
    """Chemin ou document invalide (fail-closed : l'appelant repond en erreur)."""


def racine_physique() -> Path:
    """Racine physique, surchargeable par ``EXCALIDRAW_BIBLIO_DIR`` (tests)."""
    return Path(
        os.environ.get("EXCALIDRAW_BIBLIO_DIR", "/srv/excalidraw/data/bibliotheque")
    )


def chemin_logique(relatif: str) -> str:
    """``rag/schema`` -> ``Excalidraw/rag/schema`` (affichage MCP/URLs)."""
    return f"{RACINE_LOGIQUE}/{relatif}" if relatif not in ("", ".") else RACINE_LOGIQUE


def normaliser_relatif(brut: str, *, fichier: bool) -> str:
    """Valide un chemin relatif et le rend sous forme normalisee ``a/b/c``.

    Leve ``ErreurBibliotheque`` sur : vide (fichier exige non-vide), absolu,
    ``..``, ``.``, segments vides (``a//b``), backslashes, NUL, segment trop
    long ou a caracteres interdits, et (mode fichier) extension differente de
    ``.excalidraw``.
    """
    if not isinstance(brut, str):
        raise ErreurBibliotheque("chemin invalide")
    if "\x00" in brut:
        raise ErreurBibliotheque("chemin invalide (octet NUL)")
    if "\\" in brut:
        raise ErreurBibliotheque("separateur `\\` interdit (utilisez `/`)")
    texte = brut.strip()
    if not texte or texte.startswith("/") or texte.startswith("./"):
        raise ErreurBibliotheque("le chemin doit etre relatif a Excalidraw (ex. `rag/schema.excalidraw`)")
    # ``a/./b`` et ``a//b`` : refuses (pas de normalisation silencieuse).
    if "/./" in f"/{texte}/" or "//" in texte:
        raise ErreurBibliotheque("chemin non normalise (`.` ou segment vide)")
    segments = texte.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ErreurBibliotheque("`..` et `.` interdits : restez sous Excalidraw")
        if len(seg) > LONGUEUR_MAX_SEGMENT:
            raise ErreurBibliotheque(f"segment trop long : {seg[:32]}...")
        if not _SEGMENT_OK.match(seg):
            raise ErreurBibliotheque(f"nom refuse : {seg[:64]}")
    if len(texte) > LONGUEUR_MAX_CHEMIN:
        raise ErreurBibliotheque("chemin trop long")
    if fichier and not texte.lower().endswith(EXTENSION):
        raise ErreurBibliotheque(f"le document doit se terminer par {EXTENSION}")
    return texte


def generer_autosave(prefixe: str = "dessin", *, horodatage: str | None = None,
                     alea: str | None = None) -> str:
    """Genere un chemin relatif d'autosave sous ``DOSSIER_IA``, sans collision.

    Format : ``ia/<prefixe>-<horodatage>-<alea>.excalidraw`` (ex.
    ``ia/dessin-20260916-003000-a1b2c3.excalidraw``). Le prefixe est assaini
    (minuscules, ``[^a-z0-9]`` -> ``-``, 32 caracteres max, ``dessin`` en
    repli) : une intention hostile (``../fuite``, NUL, backslash...) ne peut
    ni sortir du dossier IA ni produire un segment interdit. Le resultat est
    re-valide par ``normaliser_relatif`` et la boucle anti-collision (suffixe
    aleatoire regenere, 100 essais) garantit l'absence d'ecrasement.

    ``horodatage``/``alea`` ne servent qu'aux tests (determinisme).
    """
    base = re.sub(r"[^a-z0-9]+", "-", (prefixe or "").lower()).strip("-")[:32]
    if not base:
        base = "dessin"
    horo = horodatage or time.strftime("%Y%m%d-%H%M%S")
    horo = re.sub(r"[^0-9-]+", "", horo)[:15] or time.strftime("%Y%m%d-%H%M%S")
    racine = racine_physique()
    for _ in range(100):
        suffixe = alea or secrets.token_hex(3)
        alea = None  # regenere a chaque essai en cas de collision
        candidat = f"{DOSSIER_IA}/{base}-{horo}-{suffixe}{EXTENSION}"
        relatif = normaliser_relatif(candidat, fichier=True)
        if not _resoudre(racine, relatif).exists():
            return relatif
    raise ErreurBibliotheque("autosave impossible (collisions repetees)")


def _resoudre(racine: Path, relatif: str) -> Path:
    """Joint et verifie le confinement + l'absence de liens symboliques.

    Chaque composant existant du parcours est verifie ``lstat`` (pas un lien) ;
    la cible finale resolue doit rester sous la racine.
    """
    racine_reelle = racine.resolve()
    cible = racine_reelle.joinpath(*relatif.split("/"))
    # Refuse les liens symboliques sur le parcours (y compris la cible).
    parcours = racine_reelle
    for seg in relatif.split("/"):
        parcours = parcours / seg
        if parcours.is_symlink():
            raise ErreurBibliotheque("lien symbolique interdit dans la bibliotheque")
    if cible != racine_reelle and racine_reelle not in cible.parents:
        raise ErreurBibliotheque("chemin hors de la bibliotheque")
    return cible


@dataclass(frozen=True)
class FichierInfo:
    nom: str
    chemin: str  # relatif normalise
    logique: str  # Excalidraw/...
    taille: int
    modifie: float


def lister(relatif: str = "") -> dict:
    """Liste dossiers et documents d'un dossier relatif (``""`` = racine)."""
    racine = racine_physique()
    rel = normaliser_relatif(relatif, fichier=False) if relatif not in ("", ".") else ""
    racine_reelle = racine.resolve()
    dossier = _resoudre(racine, rel) if rel else racine_reelle
    if not dossier.is_dir():
        if not rel:
            # Racine jamais initialisee : bibliotheque vide, pas une erreur.
            racine_reelle.mkdir(parents=True, exist_ok=True)
            return {"dossier": chemin_logique(""), "dossiers": [], "fichiers": []}
        raise ErreurBibliotheque(f"dossier introuvable : {chemin_logique(rel)}")
    dossiers: list[str] = []
    fichiers: list[dict] = []
    for entree in sorted(dossier.iterdir(), key=lambda e: e.name.lower()):
        if entree.is_symlink():
            continue
        if entree.is_dir():
            dossiers.append(entree.name)
        elif entree.is_file() and entree.name.lower().endswith(EXTENSION):
            stat = entree.stat()
            chemin_rel = f"{rel}/{entree.name}" if rel else entree.name
            fichiers.append(
                {"nom": entree.name, "chemin": chemin_rel,
                 "logique": chemin_logique(chemin_rel),
                 "taille": stat.st_size, "modifie": stat.st_mtime}
            )
    return {"dossier": chemin_logique(rel), "dossiers": dossiers, "fichiers": fichiers}


def creer_dossier(relatif: str) -> dict:
    """Cree un dossier (parents inclus). Idempotent."""
    racine = racine_physique()
    rel = normaliser_relatif(relatif, fichier=False)
    cible = _resoudre(racine, rel)
    cible.mkdir(parents=True, exist_ok=True)
    return {"dossier": chemin_logique(rel), "cree": True}


def valider_document(brut: str) -> dict:
    """Parse et valide un document ``.excalidraw`` standard. Retourne le dict."""
    if len(brut.encode("utf-8")) > TAILLE_MAX_DOC_OCTETS:
        raise ErreurBibliotheque("document trop volumineux (5 Mo max)")
    try:
        doc = json.loads(brut)
    except ValueError:
        raise ErreurBibliotheque("document JSON invalide")
    if not isinstance(doc, dict) or doc.get("type") != "excalidraw":
        raise ErreurBibliotheque('document non standard : {"type": "excalidraw", ...} attendu')
    elements = doc.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ErreurBibliotheque("document sans elements")
    return doc


def construire_document(elements: list, app_state: dict | None = None, source: str = "") -> dict:
    """Construit un document ``.excalidraw`` standard depuis des elements.

    Les pseudo-elements non-scene (``cameraUpdate`` upstream, ...) sont
    retires : un document vide apres nettoyage est refuse (fail-closed).
    """
    if not isinstance(elements, list) or not elements:
        raise ErreurBibliotheque("aucun element a enregistrer")
    scene = nettoyer_elements(elements)
    if not scene:
        raise ErreurBibliotheque("aucun element de scene a enregistrer")
    return {
        "type": "excalidraw",
        "version": 2,
        "source": source or "https://mymcps.duckdns.org/excalidraw/bibliotheque",
        "elements": scene,
        "appState": app_state if isinstance(app_state, dict) else {},
        "files": {},
    }


def enregistrer(relatif: str, document: dict) -> dict:
    """Ecrit atomiquement un document (tmp + rename, meme systeme de fichiers).

    Les pseudo-elements non-scene (``cameraUpdate`` upstream, ...) sont
    retires avant validation/ecriture : ils ne sont jamais persistés, quel
    que soit le chemin d'ecriture (API, outils MCP, autosave).
    """
    racine = racine_physique()
    rel = normaliser_relatif(relatif, fichier=True)
    if isinstance(document, dict) and isinstance(document.get("elements"), list):
        document = {**document, "elements": nettoyer_elements(document["elements"])}
    valider_document(json.dumps(document, ensure_ascii=False))
    cible = _resoudre(racine, rel)
    cible.parent.mkdir(parents=True, exist_ok=True)
    if racine.resolve() not in (cible.parent.resolve(), *cible.parent.resolve().parents) and cible.parent.resolve() != racine.resolve():
        raise ErreurBibliotheque("chemin hors de la bibliotheque")
    contenu = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    if len(contenu) > TAILLE_MAX_DOC_OCTETS:
        raise ErreurBibliotheque("document trop volumineux (5 Mo max)")
    tmp = cible.parent / f".{cible.name}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(contenu)
        os.replace(tmp, cible)  # atomique, meme fs (pas d'EXDEV)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return {"fichier": chemin_logique(rel), "taille": len(contenu), "modifie": time.time()}


def charger(relatif: str) -> dict:
    """Lit un document et retourne ``{"fichier": logique, "document": {...}}``."""
    racine = racine_physique()
    rel = normaliser_relatif(relatif, fichier=True)
    cible = _resoudre(racine, rel)
    try:
        brut = cible.read_bytes()
    except OSError:
        raise ErreurBibliotheque(f"fichier introuvable : {chemin_logique(rel)}")
    return {"fichier": chemin_logique(rel), "document": valider_document(brut.decode("utf-8"))}


def deplacer(source: str, destination: str) -> dict:
    """Renomme/deplace un document ou un dossier (meme fs, atomique)."""
    racine = racine_physique()
    rel_src = normaliser_relatif(source, fichier=False)
    # La destination peut etre fichier ou dossier : on detecte a l'extension.
    est_fichier = rel_src.lower().endswith(EXTENSION) or destination.lower().endswith(EXTENSION)
    rel_dst = normaliser_relatif(destination, fichier=est_fichier)
    src = _resoudre(racine, rel_src)
    dst = _resoudre(racine, rel_dst)
    if not src.exists() or src.is_symlink():
        raise ErreurBibliotheque(f"source introuvable : {chemin_logique(rel_src)}")
    if dst.exists():
        raise ErreurBibliotheque(f"destination existante : {chemin_logique(rel_dst)}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return {"de": chemin_logique(rel_src), "vers": chemin_logique(rel_dst)}
