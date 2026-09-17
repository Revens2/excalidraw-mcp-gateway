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


# Types d'elements de scene connus (les autres dicts sont abandonnes :
# l'editeur officiel ne sait pas les rendre).
TYPES_SCENE = frozenset({
    "rectangle", "ellipse", "diamond", "text", "arrow", "line",
    "freedraw", "image", "frame", "magicframe", "embeddable",
})

# Champs geometriques exiges par famille (les textes standalone sans metriques
# recoivent une estimation serveur ; l'editeur affine via le `restore`
# officiel avec `refreshDimensions: true`).
_GEOMETRIE_FORME = ("x", "y", "width", "height")
_GEOMETRIE_TEXTE = ("x", "y")

# Shorthand `label` de l'API squelette upstream (`convertToExcalidrawElements`,
# ex. `{"type": "rectangle", ..., "label": {"text": "RAG", "fontSize": 20}}`).
# Constate 2026-09-17 (E2E ChatGPT reel) : `create_view` l'affiche dans la
# preview (rendu SVG tolerant) mais le `.excalidraw` persiste tel quel est
# ignore par l'editeur officiel 0.18 (`restore` ne connait pas `label` :
# formes restaurees, textes absents). Upstream confirme :
# - excalidraw/excalidraw-mcp#22 : `label` n'est PAS du format natif ; les
#   textes exigent `containerId` + `boundElements` + champs (`fontFamily`,
#   `originalText`, `autoResize`, `lineHeight`, `width`/`height`, ...) ;
# - PR #51 : `restore({refreshDimensions: true})` avant export (deja actif
#   cote editeur) ;
# - excalidraw/excalidraw (skeleton) : `label` n'existe que pour
#   `convertToExcalidrawElements`, jamais persiste ;
# - plus.excalidraw.com (scene-content-schema) : label persiste = element
#   texte separe + bindings.
# Correctif : expansion serveur en texte lie officiel (anciens + nouveaux
# fichiers, tous chemins d'ecriture via `restaurer_elements`). Idempotent :
# un document deja officiel (texte lie present) ne recoit aucun doublon,
# le shorthand residuel est simplement retire.
_TYPES_A_LABEL = frozenset({"rectangle", "ellipse", "diamond", "arrow", "line"})
_POLICE_DEFAUT = 20
_COULEUR_TEXTE_DEFAUT = "#1e1e1e"


def _extraire_label(brut: dict) -> dict | None:
    """Extrait un shorthand `label` valide, sinon None (label vide/invalide ignore)."""
    label = brut.get("label")
    if not isinstance(label, dict):
        return None
    texte = label.get("text")
    if not isinstance(texte, str) or not texte.strip():
        return None
    taille = label.get("fontSize")
    if not _est_fini(taille) or not 1 <= float(taille) <= 300:
        taille = _POLICE_DEFAUT
    else:
        taille = int(float(taille))
    couleur = label.get("strokeColor")
    if not isinstance(couleur, str) or not couleur:
        couleur = brut.get("strokeColor") if isinstance(brut.get("strokeColor"), str) else _COULEUR_TEXTE_DEFAUT
    align = label.get("textAlign") if label.get("textAlign") in ("left", "center", "right") else "center"
    valign = label.get("verticalAlign") if label.get("verticalAlign") in ("top", "middle", "bottom") else "middle"
    return {"text": texte, "fontSize": taille, "strokeColor": couleur,
            "textAlign": align, "verticalAlign": valign}


def _estimer_taille_texte(texte: str, taille: int) -> tuple[float, float]:
    """Estimation prudente (police Virgil ~0.6em/caractere) ; l'editeur affine."""
    lignes = texte.split("\n") or [texte]
    larg_max = max((len(l) for l in lignes), default=0)
    larg = max(10.0, larg_max * float(taille) * 0.6)
    haut = max(float(taille) * 1.25, len(lignes) * float(taille) * 1.25)
    return (larg, haut)


def _est_fini(valeur) -> bool:
    return isinstance(valeur, (int, float)) and not isinstance(valeur, bool) \
        and valeur == valeur and abs(valeur) != float("inf")


def _nouvel_identifiant() -> str:
    return "e" + secrets.token_hex(4)


def restaurer_element(brut: dict, position: int) -> dict | None:
    """Complete un element minimaliste upstream en element de scene standard.

    Les checkpoints upstream (`create_view`/`read_checkpoint`) ne portent que
    le strict necessaire (type, x, y, ...), sans les champs qu'Excalidraw exige
    pour rendre (seed, version, boundElements, ...) : persistés tels quels, la
    scene reste vide dans l'editeur officiel (canvas blanc, constate live
    2026-09-17). Cette fonction ne remplit que les champs surs cote serveur
    (identite, listes structurelles, defauts triviaux) sans jamais ecraser
    une valeur fournie ; l'editeur applique ensuite le `restore` officiel
    (metriques de texte, bindings). Retourne None si l'element est
    inexploitable (type inconnu, geometrie absurde, texte sans texte,
    image sans fichier).
    """
    if not isinstance(brut, dict):
        return None
    type_el = brut.get("type")
    if type_el not in TYPES_SCENE:
        return None
    if type_el == "text" and not isinstance(brut.get("text"), str):
        return None
    requis = _GEOMETRIE_TEXTE if type_el == "text" else _GEOMETRIE_FORME
    for cle in requis:
        if not _est_fini(brut.get(cle)):
            return None
    if type_el == "image" and not isinstance(brut.get("fileId"), str):
        return None
    element = dict(brut)
    identifiant = element.get("id")
    element["id"] = identifiant if isinstance(identifiant, str) and identifiant else _nouvel_identifiant()
    if not isinstance(element.get("seed"), int):
        element["seed"] = secrets.randbits(31)
    if not isinstance(element.get("version"), int):
        element["version"] = 1
    if not isinstance(element.get("versionNonce"), int):
        element["versionNonce"] = secrets.randbits(31)
    element["isDeleted"] = element.get("isDeleted") is True
    element["groupIds"] = element["groupIds"] if isinstance(element.get("groupIds"), list) else []
    element["boundElements"] = element["boundElements"] \
        if isinstance(element.get("boundElements"), list) else []
    if not isinstance(element.get("link"), (str, type(None))):
        element["link"] = None
    element["locked"] = element.get("locked") is True
    if not _est_fini(element.get("angle")):
        element["angle"] = 0
    if not _est_fini(element.get("opacity")):
        element["opacity"] = 100
    if element.get("frameId") is not None and not isinstance(element.get("frameId"), str):
        element["frameId"] = None
    if not isinstance(element.get("index"), str):
        element["index"] = f"a{position}"
    element["updated"] = int(time.time() * 1000)
    if type_el in ("arrow", "line") and not isinstance(element.get("points"), list):
        element["points"] = [[0, 0], [float(element.get("width", 0)), float(element.get("height", 0))]]
    if type_el == "text":
        # Champs texte officiels (excalidraw-mcp#22) : jamais ecrases si fournis.
        # Sans eux excalidraw.com ignore silencieusement l'element.
        texte_brut = element.get("text")
        taille = element.get("fontSize")
        if not _est_fini(taille) or not 1 <= float(taille) <= 300:
            element["fontSize"] = _POLICE_DEFAUT
        else:
            element["fontSize"] = int(float(taille))
        if not isinstance(element.get("fontFamily"), int):
            element["fontFamily"] = 1
        conteneur = element.get("containerId")
        if not isinstance(conteneur, (str, type(None))):
            element["containerId"] = None
            conteneur = None
        if not isinstance(element.get("textAlign"), str):
            element["textAlign"] = "center" if conteneur else "left"
        if not isinstance(element.get("verticalAlign"), str):
            element["verticalAlign"] = "middle" if conteneur else "top"
        if not isinstance(element.get("originalText"), str):
            element["originalText"] = texte_brut if isinstance(texte_brut, str) else ""
        if not isinstance(element.get("autoResize"), bool):
            element["autoResize"] = True
        if not _est_fini(element.get("lineHeight")):
            element["lineHeight"] = 1.25
        if not isinstance(element.get("strokeColor"), str):
            element["strokeColor"] = _COULEUR_TEXTE_DEFAUT
        if not isinstance(element.get("backgroundColor"), str):
            element["backgroundColor"] = "transparent"
        if not _est_fini(element.get("width")) or not _est_fini(element.get("height")):
            larg, haut = _estimer_taille_texte(
                texte_brut if isinstance(texte_brut, str) else "",
                int(element["fontSize"]),
            )
            if not _est_fini(element.get("width")):
                element["width"] = larg
            if not _est_fini(element.get("height")):
                element["height"] = haut
    return element


def _texte_lie_existe(restaures: list, par_id: dict, conteneur: dict) -> dict | None:
    """Retourne le texte officiel deja lie au conteneur, sinon None (anti-doublon)."""
    cid = conteneur.get("id")
    for ref in conteneur.get("boundElements") or []:
        if isinstance(ref, dict) and ref.get("type") == "text":
            cible = par_id.get(ref.get("id"))
            if (isinstance(cible, dict) and cible.get("type") == "text"
                    and cible.get("containerId") == cid):
                return cible
    for cand in restaures:
        if (isinstance(cand, dict) and cand.get("type") == "text"
                and cand.get("containerId") == cid):
            return cand
    return None


def restaurer_elements(elements: list) -> list:
    """Nettoie + restaure une liste d'elements ; vide si rien d'exploitable.

    Expansion `label` (squelette upstream) -> texte lie officiel : chaque
    conteneur `rectangle`/`ellipse`/`diamond`/`arrow`/`line` porteur d'un
    `label:{text,...}` recoit un element `text` separe (`containerId` retour
    + `boundElements` aller, texte centre). Les documents deja officiels
    (texte lie present) sont laisses intacts, sans doublon : le shorthand
    residuel est simplement retire. Les textes orphelins (`containerId`
    sans conteneur) repassent standalone (`containerId: None`) pour ne pas
    etre ignores par l'editeur.
    """
    nettoyes = nettoyer_elements(elements)
    restaures = []
    for position, brut in enumerate(nettoyes):
        element = restaurer_element(brut, position)
        if element is not None:
            restaures.append(element)
    par_id = {e["id"]: e for e in restaures if isinstance(e.get("id"), str)}
    ajouts: list[dict] = []
    for conteneur in restaures:
        if conteneur.get("type") not in _TYPES_A_LABEL:
            conteneur.pop("label", None)
            continue
        info = _extraire_label(conteneur)
        if info is None:
            conteneur.pop("label", None)
            continue
        existant = _texte_lie_existe(restaures + ajouts, par_id, conteneur)
        if existant is not None:
            # Deja officiel : pas de doublon, on retire juste le shorthand.
            conteneur.pop("label", None)
            # Reparation du aller si le texte etait orphelin du binding.
            refs = conteneur.get("boundElements")
            if isinstance(refs, list) and not any(
                isinstance(r, dict) and r.get("id") == existant.get("id") for r in refs
            ):
                refs.append({"id": existant.get("id"), "type": "text"})
            continue
        texte, taille = info["text"], info["fontSize"]
        larg, haut = _estimer_taille_texte(texte, taille)
        if conteneur["type"] in ("arrow", "line"):
            # Etiquette de fleche/ligne : centree sur le milieu geometrique.
            mx = float(conteneur.get("x", 0)) + float(conteneur.get("width", 0)) / 2.0
            my = float(conteneur.get("y", 0)) + float(conteneur.get("height", 0)) / 2.0
            tx, ty = mx - larg / 2.0, my - haut / 2.0
        else:
            tx = float(conteneur["x"]) + (float(conteneur["width"]) - larg) / 2.0
            ty = float(conteneur["y"]) + (float(conteneur["height"]) - haut) / 2.0
        nouvel_id = f"{conteneur['id']}_label"
        compteur = 2
        while nouvel_id in par_id:
            nouvel_id = f"{conteneur['id']}_label{compteur}"
            compteur += 1
        brut_texte = {
            "id": nouvel_id,
            "type": "text",
            "x": tx, "y": ty, "width": larg, "height": haut,
            "text": texte, "originalText": texte,
            "fontSize": taille, "fontFamily": 1,
            "strokeColor": info["strokeColor"],
            "textAlign": info["textAlign"], "verticalAlign": info["verticalAlign"],
            "containerId": conteneur["id"], "autoResize": True, "lineHeight": 1.25,
        }
        lie = restaurer_element(brut_texte, len(restaures) + len(ajouts))
        if lie is None:  # ne devrait pas arriver (champs tous fournis)
            conteneur.pop("label", None)
            continue
        lie["index"] = f"a{len(restaures) + len(ajouts)}"
        ajouts.append(lie)
        par_id[nouvel_id] = lie
        refs = conteneur.get("boundElements")
        if not isinstance(refs, list):
            conteneur["boundElements"] = refs = []
        refs.append({"id": nouvel_id, "type": "text"})
        conteneur.pop("label", None)
    # Reparation des bindings orphelins : texte lie sans conteneur connu ->
    # standalone (l'editeur ignore les containerId pendants) ; texte lie avec
    # conteneur connu -> aller manquant complete.
    ids_connus = set(par_id)
    for el in restaures + ajouts:
        if el.get("type") != "text":
            continue
        cid = el.get("containerId")
        if cid is None:
            continue
        cible = par_id.get(cid)
        if cible is None:
            el["containerId"] = None
            if el.get("textAlign") == "center":
                pass  # position conservee, simple repli standalone
            continue
        refs = cible.get("boundElements")
        if not isinstance(refs, list):
            cible["boundElements"] = refs = []
        if not any(isinstance(r, dict) and r.get("id") == el.get("id") for r in refs):
            refs.append({"id": el.get("id"), "type": "text"})
    _ = ids_connus
    return restaures + ajouts


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
    retires et les elements minimalistes sont restaures (champs exiges par
    l'editeur officiel) : un document vide apres nettoyage/restauration est
    refuse (fail-closed).
    """
    if not isinstance(elements, list) or not elements:
        raise ErreurBibliotheque("aucun element a enregistrer")
    scene = restaurer_elements(elements)
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
    retires et les elements minimalistes restaures avant validation/ecriture :
    ils ne sont jamais persistés tels quels, quel que soit le chemin
    d'ecriture (API, outils MCP, autosave).
    """
    racine = racine_physique()
    rel = normaliser_relatif(relatif, fichier=True)
    if isinstance(document, dict) and isinstance(document.get("elements"), list):
        document = {**document, "elements": restaurer_elements(document["elements"])}
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
