"""Outils MCP servis localement par la passerelle (bibliotheque distante).

Ces outils ne sont JAMAIS relayes vers l'upstream : ``ProxyMCP`` les annonce
dans ``tools/list`` (selon les portees du jeton, voir ``politique.py``) et les
execute ici, sur la racine durable ``/srv/excalidraw/data/bibliotheque``
presentee comme ``Excalidraw``.

``create_view`` reste un outil upstream, mais sa definition annoncee est
enrichie d'un parametre optionnel ``enregistrer_sous`` : avec lui, la
passerelle relaye vers l'upstream, relit le checkpoint genere, persiste un
``.excalidraw`` standard au chemin demande (prioritaire) ; sans lui,
autosave automatique sous ``Excalidraw/ia/`` (nom horodate sans collision).
Dans les deux cas la reponse est enrichie d'un identifiant/URL interne
``ouvrir dans Excalidraw`` (editeur principal), et le rendu upstream n'est
jamais sacrifie : un echec de persistance est signale dans la reponse, rendu
preserve.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

from excalidraw_gateway import bibliotheque
from excalidraw_gateway.bibliotheque import ErreurBibliotheque

# Parametre d'extension de create_view (persistance du diagramme genere).
PARAM_PERSISTANCE = "enregistrer_sous"

# URL de base de l'editeur (sans le deep-link ``#/<chemin>``), parametrable
# sans toucher au code : en production privee, vaut l'editeur du vhost
# NetBird (ex. ``http://10.200.114.203:8130/editeur``). Absente, on
# retombe sur le montage public historique (compatibilite).
NOM_ENV_URL_EDITEUR = "EXCALIDRAW_URL_EDITEUR"

COMPTEUR_APPELS = "biblio_appel_local"


@dataclass(frozen=True)
class ContexteLocal:
    """Ce dont les outils locaux ont besoin : upstream + base d'ouverture."""

    upstream: str  # ex. http://127.0.0.1:8122 (sans /mcp final)
    base_ouverture: str  # ex. https://mymcps.duckdns.org (sans slash final)


def url_ouverture(ctx: ContexteLocal, chemin_relatif: str) -> str:
    """URL interne « ouvrir dans Excalidraw » pour un chemin relatif.

    Pointe vers l'editeur principal integre, avec deep-link ``#/<chemin>``
    charge automatiquement. Si ``EXCALIDRAW_URL_EDITEUR`` est defini
    (editeur prive NetBird, ex. ``http://10.200.114.203:8130/editeur``),
    il est utilise tel quel ; sinon, repli historique sur le montage
    public ``<base>/excalidraw/editeur``.
    """
    prefixe = os.environ.get(NOM_ENV_URL_EDITEUR, "").strip().rstrip("/")
    if prefixe:
        return f"{prefixe}#/{chemin_relatif}"
    base = (ctx.base_ouverture or "https://mymcps.duckdns.org").rstrip("/")
    return f"{base}/excalidraw/editeur#/{chemin_relatif}"


def _schema_objet(proprietes: dict, requis: list[str], description: str = "") -> dict:
    schema: dict = {"type": "object", "properties": proprietes}
    if requis:
        schema["required"] = requis
    if description:
        schema["description"] = description
    return schema


DEFINITIONS_OUTILS_LOCAUX: list[dict] = [
    {
        "name": "library_list",
        "description": (
            "Liste les dossiers et les dessins `.excalidraw` de la bibliotheque distante "
            "`Excalidraw` (persistance serveur). `dossier` est relatif a la racine "
            "(ex. `rag` ou vide pour la racine). Ne voit jamais le reste du disque."
        ),
        "inputSchema": _schema_objet(
            {"dossier": {"type": "string", "description": "Dossier relatif, vide = racine Excalidraw."}},
            [],
        ),
    },
    {
        "name": "library_mkdir",
        "description": (
            "Cree un dossier (parents inclus) dans la bibliotheque `Excalidraw`. "
            "Ex. `rag` pour y ranger des schemas."
        ),
        "inputSchema": _schema_objet(
            {"dossier": {"type": "string", "description": "Dossier relatif a creer."}},
            ["dossier"],
        ),
    },
    {
        "name": "library_save",
        "description": (
            "Enregistre un dessin dans la bibliotheque `Excalidraw` comme `.excalidraw` "
            "standard (re-ouvrable dans Excalidraw). `chemin` relatif avec extension "
            "`.excalidraw` (ex. `rag/schema-reseau.excalidraw`). Source : soit `elements` "
            "(chaine JSON d'elements Excalidraw), soit `checkpoint_id` (checkpoint "
            "`create_view`/`read_checkpoint` relu cote serveur). Renvoie le fichier "
            "logique et l'URL « ouvrir dans Excalidraw »."
        ),
        "inputSchema": _schema_objet(
            {
                "chemin": {"type": "string", "description": "Chemin relatif .excalidraw."},
                "elements": {"type": "string", "description": "Chaine JSON d'elements Excalidraw."},
                "checkpoint_id": {"type": "string", "description": "Checkpoint a persister."},
            },
            ["chemin"],
        ),
    },
    {
        "name": "library_load",
        "description": (
            "Charge un dessin `.excalidraw` de la bibliotheque `Excalidraw` (contenu JSON "
            "standard + URL d'ouverture). `chemin` relatif."
        ),
        "inputSchema": _schema_objet(
            {"chemin": {"type": "string", "description": "Chemin relatif .excalidraw."}},
            ["chemin"],
        ),
    },
    {
        "name": "library_move",
        "description": "Renomme ou deplace un dessin/dossier dans `Excalidraw`. Chemins relatifs.",
        "inputSchema": _schema_objet(
            {
                "source": {"type": "string", "description": "Chemin relatif existant."},
                "destination": {"type": "string", "description": "Nouveau chemin relatif."},
            },
            ["source", "destination"],
        ),
    },
]


def fusionner_definitions(resultat: dict) -> bool:
    """Injecte les definitions locales + l'extension create_view dans tools/list.

    ``resultat`` est le ``result`` d'une reponse ``tools/list`` upstream.
    Retourne True si la liste a ete modifiee (re-encodage requis).
    """
    outils = resultat.get("tools")
    if not isinstance(outils, list):
        return False
    modifie = False
    noms = {o.get("name") for o in outils if isinstance(o, dict)}
    for definition in DEFINITIONS_OUTILS_LOCAUX:
        if definition["name"] not in noms:
            outils.append(json.loads(json.dumps(definition)))  # copie profonde
            modifie = True
    for outil in outils:
        if isinstance(outil, dict) and outil.get("name") == "create_view":
            if _etendre_create_view(outil):
                modifie = True
    return modifie


def _etendre_create_view(outil: dict) -> bool:
    """Ajoute le parametre optionnel ``enregistrer_sous`` a create_view."""
    schema = outil.get("inputSchema")
    if not isinstance(schema, dict):
        outil["inputSchema"] = {
            "type": "object",
            "properties": {
                "elements": {"type": "string", "description": "Chaine JSON d'elements Excalidraw."},
                PARAM_PERSISTANCE: {
                    "type": "string",
                    "description": (
                        "Persiste le diagramme genere dans la bibliotheque `Excalidraw` "
                        "(ex. `rag/schema.excalidraw`). Prioritaire quand fourni. "
                        "Sans lui, autosave automatique sous `Excalidraw/ia/`."
                    ),
                },
            },
            "required": ["elements"],
        }
        outil["description"] = (outil.get("description") or "") + (
            " Persistance serveur : `enregistrer_sous` (ex. `rag/schema.excalidraw`) "
            "persiste au chemin demande ; sans lui, autosave automatique sous "
            "`Excalidraw/ia/`. Fichier + URL d'ouverture renvoyes dans la reponse."
        )
        return True
    proprietes = schema.get("properties")
    if not isinstance(proprietes, dict) or PARAM_PERSISTANCE in proprietes:
        return False
    proprietes[PARAM_PERSISTANCE] = {
        "type": "string",
        "description": (
            "Persiste le diagramme genere dans la bibliotheque `Excalidraw` "
            "(ex. `rag/schema.excalidraw`). Prioritaire quand fourni. "
            "Sans lui, autosave automatique sous `Excalidraw/ia/`."
        ),
    }
    return True


# --- appels serveur-vers-upstream (lecture de checkpoints) ---------------------------

async def _appel_upstream(client: httpx.AsyncClient, base: str, methode: str, params: dict) -> dict:
    """Appel JSON-RPC direct a l'upstream (boucle locale, sans session)."""
    corps = {"jsonrpc": "2.0", "id": "biblio", "method": methode, "params": params}
    reponse = await client.post(
        f"{base.rstrip('/')}/mcp",
        json=corps,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    reponse.raise_for_status()
    ctype = reponse.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        donnees_brutes = "\n".join(
            ligne[5:].lstrip() if ligne.startswith("data:") else ""
            for ligne in reponse.text.splitlines()
            if ligne.startswith("data:")
        )
        enveloppe = json.loads(donnees_brutes)
    else:
        enveloppe = reponse.json()
    if "error" in enveloppe:
        raise RuntimeError(f"upstream {methode}: {enveloppe['error']}")
    resultat = enveloppe.get("result")
    if not isinstance(resultat, dict):
        raise RuntimeError(f"upstream {methode}: reponse illisible")
    return resultat


async def lire_checkpoint_elements(client: httpx.AsyncClient, base: str, checkpoint_id: str) -> list:
    """Relit les elements resolus d'un checkpoint via l'outil upstream read_checkpoint."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", checkpoint_id or ""):
        raise ErreurBibliotheque("checkpoint_id invalide")
    resultat = await _appel_upstream(
        client, base, "tools/call",
        {"name": "read_checkpoint", "arguments": {"id": checkpoint_id}},
    )
    contenu = resultat.get("content") or []
    texte = contenu[0].get("text", "") if contenu and isinstance(contenu[0], dict) else ""
    if not texte:
        raise ErreurBibliotheque(f"checkpoint introuvable : {checkpoint_id}")
    try:
        donnees = json.loads(texte)
    except ValueError:
        raise ErreurBibliotheque(f"checkpoint illisible : {checkpoint_id}")
    elements = donnees.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ErreurBibliotheque(f"checkpoint vide : {checkpoint_id}")
    return elements


def extraire_checkpoint_id(resultat: dict) -> str | None:
    """Extrait le checkpointId d'un resultat create_view upstream."""
    structure = resultat.get("structuredContent")
    if isinstance(structure, dict) and isinstance(structure.get("checkpointId"), str):
        return structure["checkpointId"]
    for item in resultat.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            m = re.search(r'Checkpoint id:\s*"([A-Za-z0-9_-]{1,64})"', item["text"])
            if m:
                return m.group(1)
    return None


def _resultat_texte(texte: str, structure: dict | None = None) -> dict:
    resultat: dict = {"content": [{"type": "text", "text": texte}]}
    if structure is not None:
        resultat["structuredContent"] = structure
    return resultat


def _resultat_erreur(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def traiter_outil_local(
    nom: str, arguments: dict, ctx: ContexteLocal, client: httpx.AsyncClient | None = None
) -> dict:
    """Execute un outil `library_*`. Leve ErreurBibliotheque si entree invalide."""
    if nom == "library_list":
        dossier = (arguments.get("dossier") or "")
        if not isinstance(dossier, str):
            raise ErreurBibliotheque("dossier invalide")
        vue = bibliotheque.lister(dossier)
        lignes = [f"Bibliotheque {vue['dossier']} :"]
        lignes += [f"  dossier/ {d}" for d in vue["dossiers"]]
        lignes += [f"  {f['logique']} ({f['taille']} o)" for f in vue["fichiers"]]
        if not vue["dossiers"] and not vue["fichiers"]:
            lignes.append("  (vide)")
        return _resultat_texte("\n".join(lignes), {"dossier": vue["dossier"], "contenu": vue})

    if nom == "library_mkdir":
        dossier = arguments.get("dossier")
        if not isinstance(dossier, str) or not dossier.strip():
            raise ErreurBibliotheque("dossier requis")
        sortie = bibliotheque.creer_dossier(dossier)
        return _resultat_texte(
            f"Dossier cree : {sortie['dossier']}.",
            {"dossier": sortie["dossier"]},
        )

    if nom == "library_save":
        chemin = arguments.get("chemin")
        if not isinstance(chemin, str) or not chemin.strip():
            raise ErreurBibliotheque("chemin requis (ex. `rag/schema.excalidraw`)")
        elements_bruts = arguments.get("elements")
        checkpoint = arguments.get("checkpoint_id")
        if isinstance(checkpoint, str) and checkpoint.strip():
            if client is None:
                raise ErreurBibliotheque("persistance depuis checkpoint indisponible ici")
            elements = await lire_checkpoint_elements(client, ctx.upstream, checkpoint.strip())
            document = bibliotheque.construire_document(elements)
        elif isinstance(elements_bruts, str) and elements_bruts.strip():
            try:
                elements = json.loads(elements_bruts)
            except ValueError:
                raise ErreurBibliotheque("elements : JSON invalide")
            document = bibliotheque.construire_document(elements)
        else:
            raise ErreurBibliotheque("fournissez `elements` (JSON) ou `checkpoint_id`")
        sortie = bibliotheque.enregistrer(chemin, document)
        url = url_ouverture(ctx, bibliotheque.normaliser_relatif(chemin, fichier=True))
        return _resultat_texte(
            f"Dessin enregistre : {sortie['fichier']} ({len(document['elements'])} elements).\n"
            f"Ouvrir dans Excalidraw : {url}",
            {"fichier": sortie["fichier"], "url_ouverture": url,
             "elements": len(document["elements"])},
        )

    if nom == "library_load":
        chemin = arguments.get("chemin")
        if not isinstance(chemin, str) or not chemin.strip():
            raise ErreurBibliotheque("chemin requis")
        sortie = bibliotheque.charger(chemin)
        url = url_ouverture(ctx, bibliotheque.normaliser_relatif(chemin, fichier=True))
        doc = sortie["document"]
        return _resultat_texte(
            f"Dessin {sortie['fichier']} : {len(doc.get('elements', []))} elements.\n"
            f"Ouvrir dans Excalidraw : {url}\n"
            f"Contenu JSON : {json.dumps(doc, ensure_ascii=False)}",
            {"fichier": sortie["fichier"], "url_ouverture": url, "document": doc},
        )

    if nom == "library_move":
        source = arguments.get("source")
        destination = arguments.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ErreurBibliotheque("source et destination requises")
        sortie = bibliotheque.deplacer(source, destination)
        return _resultat_texte(
            f"Deplace : {sortie['de']} -> {sortie['vers']}.",
            {"de": sortie["de"], "vers": sortie["vers"]},
        )

    raise ErreurBibliotheque(f"outillocal inconnu : {nom}")
