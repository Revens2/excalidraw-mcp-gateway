"""Tests de la bibliotheque distante Excalidraw (persistance serveur).

Couvre : confinement des chemins (traversal, liens symboliques), CRUD,
politique d'outils, injection tools/list, outils locaux servis sans upstream,
`create_view` systematiquement persiste (`enregistrer_sous` prioritaire,
sinon autosave `ia/` : nom sur, sans collision, rendu preserve et echec
signale), page `/editeur` (UI privee sans jeton), et API HTTP (jeton).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from excalidraw_gateway import bibliotheque
from excalidraw_gateway.app import construire_application
from excalidraw_gateway.bibliotheque import ErreurBibliotheque
from excalidraw_gateway.oauth import hacher_phrase
from excalidraw_gateway.outils_locaux import (
    NOM_ENV_URL_EDITEUR,
    ContexteLocal,
    extraire_checkpoint_id,
    fusionner_definitions,
    url_ouverture,
)
from excalidraw_gateway.politique import (
    OUTILS_BIBLIO_ECRITURE,
    OUTILS_BIBLIO_LECTURE,
    OUTILS_ECRITURE,
    OUTILS_LECTURE,
    PolitiqueOutils,
)

EMETTEUR = "https://biblio.example.test"
JETON_LECTURE = "l" * 40
JETON_ECRITURE = "e" * 40
JETON_BIBLIO = "b" * 40
PHRASE = "phrase-de-test-2026"
SCOPES_LECTURE = "excalidraw:lecture"
SCOPES_ECRITURE = "excalidraw:lecture excalidraw:ecriture"

ELEMENTS_DEMO = [
    {"type": "cameraUpdate", "width": 800, "height": 600, "x": 0, "y": 0},
    {"type": "rectangle", "id": "r1", "x": 100, "y": 100, "width": 200, "height": 80,
     "label": {"text": "RAG", "fontSize": 20}},
]

# Apres nettoyage + restauration : le pseudo-element `cameraUpdate` (cadrage
# upstream, jamais un element de scene) n'est pas persiste, et le rectangle
# minimaliste est complete (champs exiges par l'editeur officiel).
ELEMENTS_SCENE_ATTENDUS = [ELEMENTS_DEMO[1]]


def _assert_rectangle_restaure(elements):
    """Verifie la restauration serveur + l'expansion `label` -> texte lie officiel."""
    # excalidraw-mcp#22 : le shorthand `label` (squelette) n'est jamais persiste
    # tel quel ; il est expanse en element texte lie (containerId + boundElements).
    assert len(elements) == 2
    r = elements[0]
    assert r["type"] == "rectangle" and r["id"] == "r1"
    assert r["x"] == 100 and r["width"] == 200
    assert "label" not in r, "shorthand label ne doit jamais etre persiste"
    assert isinstance(r["seed"], int) and isinstance(r["versionNonce"], int)
    assert r["boundElements"] == [{"id": "r1_label", "type": "text"}] and r["groupIds"] == []
    assert r["isDeleted"] is False and r["locked"] is False
    assert r["angle"] == 0 and r["opacity"] == 100
    t = elements[1]
    assert t["type"] == "text" and t["id"] == "r1_label"
    assert t["text"] == "RAG" and t["originalText"] == "RAG"
    assert t["containerId"] == "r1" and t["fontSize"] == 20
    assert t["fontFamily"] == 1 and t["textAlign"] == "center"
    assert "label" not in t
    return r


@pytest.fixture()
def environ(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCALIDRAW_MCP_ISSUER", EMETTEUR)
    monkeypatch.setenv("EXCALIDRAW_MCP_UPSTREAM", "http://127.0.0.1:9")
    monkeypatch.setenv("EXCALIDRAW_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    monkeypatch.setenv("EXCALIDRAW_MCP_TOKEN", JETON_ECRITURE)
    monkeypatch.setenv("EXCALIDRAW_MCP_CONSENT_HASH", hacher_phrase(PHRASE))
    monkeypatch.setenv("EXCALIDRAW_BIBLIO_DIR", str(tmp_path / "biblio"))
    monkeypatch.setenv("EXCALIDRAW_BIBLIO_TOKEN", JETON_BIBLIO)
    # Les tests du montage historique ne doivent jamais voir l'URL privee.
    monkeypatch.delenv(NOM_ENV_URL_EDITEUR, raising=False)
    return tmp_path


def _courir(coro):
    return asyncio.run(coro)


def _client(jeton: str, portees: str) -> httpx.AsyncClient:
    os.environ["EXCALIDRAW_MCP_TOKEN_SCOPES"] = portees
    app = construire_application(jeton_statique=jeton)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=EMETTEUR)


def _json_rpc(methode: str, identifiant: int | None, params: dict | None = None) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": identifiant, "method": methode, "params": params or {}}
    ).encode()


def _entetes(jeton: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {jeton}",
    }


# --- confinement ----------------------------------------------------------------------

ELEMENTS_DOC = {"type": "excalidraw", "version": 2, "elements": ELEMENTS_DEMO,
                "appState": {}, "files": {}}


@pytest.mark.parametrize("mauvais", [
    "", "/absolu.excalidraw", "../fuite.excalidraw", "a/../../fuite.excalidraw",
    "a/./b.excalidraw", "a//b.excalidraw", "./relatif.excalidraw", "..",
    "a\\b.excalidraw", "nul\x00.excalidraw", "mauvais.txt", "a rocket?.excalidraw",
    "x" * 200 + ".excalidraw",
])
def test_chemins_refuses(environ, mauvais):
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.normaliser_relatif(mauvais, fichier=True)


@pytest.mark.parametrize("bon", [
    "schema.excalidraw", "rag/schema RAG (v2).excalidraw", "a/b/c.excalidraw",
    "dossier accentué/été.excalidraw",
])
def test_chemins_acceptes(environ, bon):
    assert bibliotheque.normaliser_relatif(bon, fichier=True) == bon.strip()


def test_lien_symbolique_refuse(environ):
    bibliotheque.creer_dossier("d")
    vraie = bibliotheque.racine_physique() / "d"
    (vraie / "cible.excalidraw").write_text(json.dumps(ELEMENTS_DOC))
    lien = bibliotheque.racine_physique() / "lien.excalidraw"
    try:
        lien.symlink_to(vraie / "cible.excalidraw")
    except OSError:
        pytest.skip("liens symboliques non supportes ici")
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.charger("lien.excalidraw")


def test_document_non_standard_refuse(environ):
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.valider_document('{"type": "pas-excalidraw", "elements": []}')
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.valider_document('{"type": "excalidraw", "elements": []}')
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.enregistrer("x.excalidraw", {"type": "excalidraw", "elements": []})


# --- CRUD ------------------------------------------------------------------------------

def test_crud_complet(environ):
    assert bibliotheque.lister("") == {
        "dossier": "Excalidraw", "dossiers": [], "fichiers": []}
    bibliotheque.creer_dossier("rag/sous")
    doc = bibliotheque.construire_document(ELEMENTS_DEMO)
    assert doc["type"] == "excalidraw" and doc["files"] == {}
    sortie = bibliotheque.enregistrer("rag/schema.excalidraw", doc)
    assert sortie["fichier"] == "Excalidraw/rag/schema.excalidraw"
    vue = bibliotheque.lister("rag")
    assert vue["dossiers"] == ["sous"]
    assert [f["logique"] for f in vue["fichiers"]] == ["Excalidraw/rag/schema.excalidraw"]
    relu = bibliotheque.charger("rag/schema.excalidraw")
    _assert_rectangle_restaure(relu["document"]["elements"])
    dep = bibliotheque.deplacer("rag/schema.excalidraw", "rag/schema-v2.excalidraw")
    assert dep["vers"] == "Excalidraw/rag/schema-v2.excalidraw"
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.charger("rag/schema.excalidraw")
    # Fichier brut = .excalidraw standard lisible par Excalidraw.
    brut = json.loads((bibliotheque.racine_physique() / "rag" / "schema-v2.excalidraw").read_text())
    assert brut["type"] == "excalidraw" and isinstance(brut["elements"], list)


# --- politique ----------------------------------------------------------------------------

def test_politique_biblio():
    p = PolitiqueOutils()
    lecture = {"excalidraw:lecture"}
    full = {"excalidraw:lecture", "excalidraw:ecriture"}
    assert p.visibles(lecture) == set(OUTILS_LECTURE) | set(OUTILS_BIBLIO_LECTURE)
    assert p.visibles(full) == (
        set(OUTILS_LECTURE) | set(OUTILS_ECRITURE)
        | set(OUTILS_BIBLIO_LECTURE) | set(OUTILS_BIBLIO_ECRITURE)
    )
    assert p.autoriser_call("library_list", lecture) is None
    assert p.autoriser_call("library_save", lecture) is not None
    assert p.autoriser_call("library_save", full) is None
    assert p.autoriser_call("library_move", full) is None
    assert p.autoriser_call("outil-xyz", full) is not None


def test_validate_view_est_lecture_seule():
    """`validate_view` (2026-09-22) : diagnostic geometrique, jamais une ecriture.

    L'outil relit un checkpoint et renvoie un rapport deterministe : aucune
    donnee n'est creee, aucune sortie vers l'exterieur. Il doit donc etre
    annonce et executable avec la seule portee lecture, et reste refuse sans
    portee (fail-closed comme tout le reste).
    """
    p = PolitiqueOutils()
    lecture = {SCOPES_LECTURE}
    assert "validate_view" in OUTILS_LECTURE
    assert "validate_view" not in OUTILS_ECRITURE
    assert "validate_view" in p.visibles(lecture)
    assert p.autoriser_call("validate_view", lecture) is None
    assert p.autoriser_call("validate_view", set()) is not None


def test_fusion_definitions():
    resultat = {"tools": [{"name": "create_view", "description": "rendu",
                           "inputSchema": {"type": "object",
                                           "properties": {"elements": {"type": "string"}},
                                           "required": ["elements"]}}]}
    assert fusionner_definitions(resultat) is True
    noms = {t["name"] for t in resultat["tools"]}
    assert noms >= set(OUTILS_BIBLIO_LECTURE) | set(OUTILS_BIBLIO_ECRITURE) | {"create_view"}
    cv = next(t for t in resultat["tools"] if t["name"] == "create_view")
    assert "enregistrer_sous" in cv["inputSchema"]["properties"]
    assert fusionner_definitions(resultat) is False  # idempotent


def test_extraction_checkpoint():
    assert extraire_checkpoint_id({"structuredContent": {"checkpointId": "abc1"}}) == "abc1"
    assert extraire_checkpoint_id(
        {"content": [{"type": "text", "text": 'Diagram displayed! Checkpoint id: "zz99".'}]}) == "zz99"
    assert extraire_checkpoint_id({"content": [{"type": "text", "text": "rien"}]}) is None


# --- integration MCP (stub upstream) ------------------------------------------------------

_CHECKPOINT_ELEMENTS = [{"type": "rectangle", "id": "r9", "x": 1, "y": 2,
                         "width": 10, "height": 10}]


def _serveur_stub(creer_sans_checkpoint=False):
    recus: list[dict] = []

    async def _post(request):
        corps = await request.body()
        donnees = json.loads(corps)
        methode = donnees.get("method")
        id_ = donnees.get("id")
        session = request.headers.get("mcp-session-id", "") or str(uuid.uuid4())
        if methode == "initialize":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": id_,
                 "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "serverInfo": {"name": "stub", "version": "t"}}},
                headers={"mcp-session-id": session})
        if methode == "tools/list":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": id_,
                 "result": {"tools": [
                     {"name": "read_me", "description": "r"},
                     {"name": "create_view", "description": "rendu",
                      "inputSchema": {"type": "object",
                                      "properties": {"elements": {"type": "string"}},
                                      "required": ["elements"]}},
                     {"name": "save_checkpoint", "description": "s"},
                     {"name": "read_checkpoint", "description": "r"},
                     {"name": "validate_view", "description": "v"},
                     {"name": "export_to_excalidraw", "description": "e"},
                 ]}},
                headers={"mcp-session-id": session})
        if methode == "tools/call":
            params = donnees.get("params") or {}
            nom = params.get("name")
            args = params.get("arguments") or {}
            recus.append({"name": nom, "args": args})
            if nom == "create_view":
                assert "enregistrer_sous" not in args, "le parametre local ne doit pas fuiter"
                if creer_sans_checkpoint:
                    return JSONResponse(
                        {"jsonrpc": "2.0", "id": id_,
                         "result": {
                             "content": [{"type": "text",
                                          "text": "Diagram displayed! (no checkpoint)."}],
                             "structuredContent": {}}},
                        headers={"mcp-session-id": session})
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": id_,
                     "result": {
                         "content": [{"type": "text",
                                      "text": 'Diagram displayed! Checkpoint id: "cp123".'}],
                         "structuredContent": {"checkpointId": "cp123"}}},
                    headers={"mcp-session-id": session})
            if nom == "read_checkpoint":
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": id_,
                     "result": {"content": [{"type": "text",
                                             "text": json.dumps({"elements": _CHECKPOINT_ELEMENTS})}]}}
                    ,
                    headers={"mcp-session-id": session})
            return JSONResponse(
                {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": "ok"}]}},
                headers={"mcp-session-id": session})
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {}},
                            headers={"mcp-session-id": session})

    app = Starlette(routes=[Route("/mcp", _post, methods=["POST"])])
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(128)
    serveur = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=serveur.run, kwargs={"sockets": [s]}, daemon=True)
    thread.start()
    for _ in range(300):
        if serveur.started:
            break
        time.sleep(0.02)
    return serveur, f"http://127.0.0.1:{port}", s, recus


def _session(c):
    async def _s():
        r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=_entetes(JETON_ECRITURE))
        return r.headers["mcp-session-id"]
    return _courir(_s())


def test_liste_annonce_biblio_selon_portees(environ):
    async def _t():
        serveur, url, sock, recus = _serveur_stub()
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_LECTURE, SCOPES_LECTURE) as c:
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=_entetes(JETON_LECTURE))
                session = r.headers["mcp-session-id"]
                r = await c.post("/mcp", content=_json_rpc("tools/list", 2),
                                 headers={**_entetes(JETON_LECTURE), "mcp-session-id": session})
                noms = {t["name"] for t in r.json()["result"]["tools"]}
                assert noms == set(OUTILS_LECTURE) | set(OUTILS_BIBLIO_LECTURE), noms
                cv = next(t for t in r.json()["result"]["tools"] if t["name"] == "create_view") \
                    if "create_view" in noms else None
                assert cv is None  # lecture seule : pas de create_view
            async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=_entetes(JETON_ECRITURE))
                session = r.headers["mcp-session-id"]
                r = await c.post("/mcp", content=_json_rpc("tools/list", 2),
                                 headers={**_entetes(JETON_ECRITURE), "mcp-session-id": session})
                noms = {t["name"] for t in r.json()["result"]["tools"]}
                assert noms == (set(OUTILS_LECTURE) | set(OUTILS_ECRITURE)
                                | set(OUTILS_BIBLIO_LECTURE) | set(OUTILS_BIBLIO_ECRITURE)), noms
                cv = next(t for t in r.json()["result"]["tools"] if t["name"] == "create_view")
                assert "enregistrer_sous" in cv["inputSchema"]["properties"]
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


def test_outils_locaux_sans_upstream(environ):
    async def _t():
        serveur, url, sock, recus = _serveur_stub()
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
                ent = _entetes(JETON_ECRITURE)
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=ent)
                session = r.headers["mcp-session-id"]
                h = {**ent, "mcp-session-id": session}

                def _call(i, nom, args):
                    return c.post("/mcp", content=_json_rpc(
                        "tools/call", i, {"name": nom, "arguments": args}), headers=h)

                r = await _call(3, "library_mkdir", {"dossier": "rag"})
                assert "Excalidraw/rag" in r.text, r.text
                r = await _call(4, "library_save", {
                    "chemin": "rag/schema-reseau.excalidraw",
                    "elements": json.dumps(ELEMENTS_DEMO)})
                assert "url_ouverture" in r.text and "Excalidraw/rag/schema-reseau.excalidraw" in r.text
                r = await _call(5, "library_list", {"dossier": "rag"})
                assert "schema-reseau.excalidraw" in r.text
                r = await _call(6, "library_load", {"chemin": "rag/schema-reseau.excalidraw"})
                assert "RAG" in r.text
                r = await _call(7, "library_move",
                                {"source": "rag/schema-reseau.excalidraw", "destination": "rag/schema-v2.excalidraw"})
                assert "schema-v2" in r.text
                # Traversal refuse localement.
                r = await _call(8, "library_load", {"chemin": "../fuite.excalidraw"})
                assert r.json()["result"]["isError"] is True
                # L'upstream n'a recu QUE initialize (jamais les library_*).
                assert [a["name"] for a in recus] == []
                # Fichier durable reel, standard.
                contenu = json.loads(
                    (bibliotheque.racine_physique() / "rag" / "schema-v2.excalidraw").read_text())
                assert contenu["type"] == "excalidraw"
                _assert_rectangle_restaure(contenu["elements"])
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


def test_create_view_persistant_bout_en_bout(environ):
    async def _t():
        serveur, url, sock, recus = _serveur_stub()
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
                ent = _entetes(JETON_ECRITURE)
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=ent)
                session = r.headers["mcp-session-id"]
                h = {**ent, "mcp-session-id": session}
                r = await c.post("/mcp", content=_json_rpc(
                    "tools/call", 3, {"name": "create_view",
                                      "arguments": {"elements": json.dumps(ELEMENTS_DEMO),
                                                    "enregistrer_sous": "rag/ia.excalidraw"}}),
                    headers=h)
                corps = r.json()
                assert "error" not in corps, corps
                texte = corps["result"]["content"][0]["text"]
                assert "Checkpoint" in texte and "Excalidraw/rag/ia.excalidraw" in texte
                assert "url_ouverture" in corps["result"]["structuredContent"]
                assert corps["result"]["structuredContent"]["fichier"] == "Excalidraw/rag/ia.excalidraw"
                assert corps["result"]["structuredContent"]["persistance_ok"] is True
                assert corps["result"]["structuredContent"]["enregistrement_automatique"] is False
                assert "/excalidraw/editeur#/" in corps["result"]["structuredContent"]["url_ouverture"]
                # Le parametre local n'a pas fuite vers l'upstream.
                assert recus[0]["name"] == "create_view"
                assert "enregistrer_sous" not in recus[0]["args"]
                # Fichier = elements resolus du checkpoint (r9), restaures
                # (le stub renvoie un rectangle minimaliste, complete a l'ecriture).
                relu = bibliotheque.charger("rag/ia.excalidraw")
                assert len(relu["document"]["elements"]) == 1
                r9 = relu["document"]["elements"][0]
                assert (r9["id"], r9["x"], r9["width"]) == ("r9", 1, 10)
                assert isinstance(r9["seed"], int) and r9["boundElements"] == []
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


def test_autosave_nom_sur_et_sans_collision(environ):
    p1 = bibliotheque.generer_autosave(horodatage="20260916-003000", alea="a1b2c3")
    assert p1 == "ia/dessin-20260916-003000-a1b2c3.excalidraw"
    # Prefixe hostile assaini : confine sous ia/, aucun segment interdit.
    hostile = bibliotheque.generer_autosave(
        prefixe="../../fuite\x00\\X", horodatage="20260916-003000", alea="a1b2c3")
    assert hostile.startswith("ia/") and ".." not in hostile and "\\" not in hostile
    bibliotheque.normaliser_relatif(hostile, fichier=True)
    # Prefixe vide ou symboles seuls -> repli "dessin".
    assert bibliotheque.generer_autosave(
        prefixe="!!!", horodatage="20260916-003000", alea="zz").startswith("ia/dessin-")
    # Collision -> nouveau suffixe, jamais d'ecrasement.
    doc = bibliotheque.construire_document(ELEMENTS_DEMO)
    bibliotheque.enregistrer(p1, doc)
    p2 = bibliotheque.generer_autosave(horodatage="20260916-003000", alea="a1b2c3")
    assert p2 != p1 and p2.startswith("ia/dessin-20260916-003000-")
    bibliotheque.normaliser_relatif(p2, fichier=True)


def test_create_view_autosave_par_defaut(environ):
    """Sans `enregistrer_sous` : autosave sous Excalidraw/ia/ + URL editeur principal."""
    async def _t():
        serveur, url, sock, recus = _serveur_stub()
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
                ent = _entetes(JETON_ECRITURE)
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=ent)
                session = r.headers["mcp-session-id"]

                def _creer(i):
                    return c.post("/mcp", content=_json_rpc(
                        "tools/call", i, {"name": "create_view",
                                          "arguments": {"elements": json.dumps(ELEMENTS_DEMO)}}),
                        headers={**ent, "mcp-session-id": session})

                r = await _creer(3)
                corps = r.json()
                assert "error" not in corps, corps
                texte = corps["result"]["content"][0]["text"]
                # Rendu preserve + mention du fichier auto.
                assert "Checkpoint" in texte and "Excalidraw/ia/" in texte
                structure = corps["result"]["structuredContent"]
                assert structure["persistance_ok"] is True
                assert structure["enregistrement_automatique"] is True
                assert structure["fichier"].startswith("Excalidraw/ia/")
                assert structure["fichier"].endswith(".excalidraw")
                assert "/excalidraw/editeur#/" in structure["url_ouverture"]
                # Le parametre local ne fuit jamais vers l'upstream.
                assert recus[0]["name"] == "create_view"
                assert "enregistrer_sous" not in recus[0]["args"]
                # Fichier = elements resolus du checkpoint (r9), restaures.
                relu = bibliotheque.charger(structure["fichier"].removeprefix("Excalidraw/"))
                assert len(relu["document"]["elements"]) == 1
                assert relu["document"]["elements"][0]["id"] == "r9"
                # Second appel sans chemin -> second fichier (pas d'ecrasement).
                r2 = await _creer(4)
                f2 = r2.json()["result"]["structuredContent"]["fichier"]
                assert f2 != structure["fichier"] and f2.startswith("Excalidraw/ia/")
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


def test_create_view_persistance_signalee_rendu_preserve(environ):
    """Rendu sans checkpoint : echec signale, rendu intact, aucun fichier."""
    async def _t():
        serveur, url, sock, recus = _serveur_stub(creer_sans_checkpoint=True)
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
                ent = _entetes(JETON_ECRITURE)
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=ent)
                session = r.headers["mcp-session-id"]
                r = await c.post("/mcp", content=_json_rpc(
                    "tools/call", 3, {"name": "create_view",
                                      "arguments": {"elements": json.dumps(ELEMENTS_DEMO)}}),
                    headers={**ent, "mcp-session-id": session})
                corps = r.json()
                assert "error" not in corps, corps
                texte = corps["result"]["content"][0]["text"]
                assert "Diagram displayed!" in texte  # rendu preserve
                assert "Persistance distante echouee" in texte  # echec signale
                assert corps["result"]["structuredContent"]["persistance_ok"] is False
                assert list((bibliotheque.racine_physique()).glob("**/*.excalidraw")) == []
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


def test_page_editeur_ui_privee_sans_jeton(environ):
    """Editeur : CSS officiel, actions discretes, aucun secret navigateur."""
    async def _t():
        async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
            r = await c.get("/editeur")
            assert r.status_code == 200, r.status_code
            # Feuille de style officielle du composant (diagnostic 2026-09-16 :
            # son absence produisait le rendu brut).
            assert "@excalidraw/excalidraw@0.18.0/dist/prod/index.css" in r.text
            assert 'integrity="sha384-' in r.text  # SRI epargne CDN altere
            assert "@excalidraw/excalidraw@0.18.0" in r.text
            # Actions bibliotheque discretes integrees au canvas officiel.
            assert "renderTopRightUI" in r.text
            for action in ("Ouvrir", "Enregistrer", "Nouveau"):
                assert action in r.text, action
            # Aucun secret cote navigateur : ni champ, ni stockage, ni header.
            assert "excali_jeton" not in r.text
            assert "localStorage" not in r.text
            assert "Authorization" not in r.text
            assert "EXCALIDRAW_BIBLIO_TOKEN" not in r.text
            assert 'type="password"' not in r.text
            # Deep-link MCP supporte.
            assert 'location.hash.startsWith("#/")' in r.text

    _courir(_t())


def test_page_bibliotheque_redirige_editeur(environ):
    """L'ancienne page /bibliotheque redirige vers l'editeur integre."""
    async def _t():
        async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
            r = await c.get("/bibliotheque", follow_redirects=False)
            assert r.status_code in (301, 302), r.status_code
            assert r.headers["location"] == "/editeur"

    _courir(_t())


def test_construire_document_filtre_camera_update(environ):
    """Le pseudo-element `cameraUpdate` upstream n'est jamais persiste."""
    doc = bibliotheque.construire_document(ELEMENTS_DEMO)
    _assert_rectangle_restaure(doc["elements"])
    assert all(e.get("type") != "cameraUpdate" for e in doc["elements"])
    # Checkpoint ne contenant QUE du cadrage -> refuse (fail-closed),
    # l'appelant signale l'echec en preservant le rendu.
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.construire_document([ELEMENTS_DEMO[0]])
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.construire_document([])


def test_enregistrer_filtre_camera_update_quel_que_soit_le_chemin(environ):
    """`enregistrer` (API PUT, outils, autosave) ne persiste jamais cameraUpdate."""
    doc = {"type": "excalidraw", "version": 2, "elements": ELEMENTS_DEMO,
           "appState": {}, "files": {}}
    bibliotheque.enregistrer("rag/nettoye.excalidraw", doc)
    relu = bibliotheque.charger("rag/nettoye.excalidraw")
    _assert_rectangle_restaure(relu["document"]["elements"])
    # Document ne contenant que du cadrage -> 400 (fail-closed).
    with pytest.raises(ErreurBibliotheque):
        bibliotheque.enregistrer("rag/vide.excalidraw",
                                 {"type": "excalidraw", "version": 2,
                                  "elements": [ELEMENTS_DEMO[0]],
                                  "appState": {}, "files": {}})


def test_restaurer_element_minimaliste_upstream(environ):
    """Elements minimalistes upstream -> scene standard (valeurs preservees)."""
    sparse = {"type": "rectangle", "id": "z", "x": 25, "y": 90,
              "width": 1550, "height": 160, "strokeColor": "#f59e0b",
              "roundness": {"type": 3}}
    r = bibliotheque.restaurer_element(dict(sparse), 0)
    assert r["x"] == 25 and r["width"] == 1550 and r["roundness"] == {"type": 3}
    assert isinstance(r["seed"], int) and r["isDeleted"] is False
    assert r["boundElements"] == [] and r["index"] == "a0"
    # Texte minimaliste : complete en texte officiel (estimation serveur,
    # l'editeur affine via restore+refreshDimensions).
    t = bibliotheque.restaurer_element(
        {"type": "text", "x": 1, "y": 2, "text": "hello", "fontSize": 20}, 3)
    assert t["text"] == "hello" and t["originalText"] == "hello"
    assert t["fontFamily"] == 1 and t["containerId"] is None
    assert t["width"] > 0 and t["height"] > 0 and t["index"] == "a3"
    # Fleche sans points : repli geometrique.
    f = bibliotheque.restaurer_element(
        {"type": "arrow", "x": 0, "y": 0, "width": 10, "height": 5}, 0)
    assert f["points"] == [[0, 0], [10.0, 5.0]]
    # Inexploitables : type inconnu, geometrie absurde, texte sans texte,
    # image sans fichier, cameraUpdate.
    assert bibliotheque.restaurer_element({"type": "nope", "x": 0, "y": 0}, 0) is None
    assert bibliotheque.restaurer_element({"type": "rectangle", "x": 0}, 0) is None
    assert bibliotheque.restaurer_element({"type": "text", "x": 0, "y": 0}, 0) is None
    assert bibliotheque.restaurer_element(
        {"type": "image", "x": 0, "y": 0, "width": 1, "height": 1}, 0) is None
    assert bibliotheque.restaurer_elements(ELEMENTS_DEMO) != []
    assert all(e["type"] != "cameraUpdate"
               for e in bibliotheque.restaurer_elements(ELEMENTS_DEMO))
    assert bibliotheque.restaurer_elements([{"type": "cameraUpdate"}]) == []


def test_url_ouverture_encode_espaces(environ, monkeypatch):
    """Deep-link : espaces et caracteres usuels encodes par segment."""
    ctx = ContexteLocal(upstream="http://127.0.0.1:9", base_ouverture="https://biblio.example.test")
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur")
    assert url_ouverture(ctx, "rag/mon schema (v2).excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/mon%20schema%20%28v2%29.excalidraw"
    # Chemins simples inchanges (compatibilite des liens existants).
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/schema.excalidraw"


def test_page_editeur_correctifs_2026_09_17(environ):
    """Editeur : favicon, icones modale, deep-link robuste, cadrage natif."""
    async def _t():
        async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
            r = await c.get("/editeur")
            assert r.status_code == 200, r.status_code
            # Bug 1 — favicon : logomark inline, zero requete reseau.
            assert 'rel="icon"' in r.text and "data:image/svg+xml" in r.text
            # Bug 2 — icones modale contraintes explicitement.
            assert ".entree svg" in r.text and "20px" in r.text
            assert ".modale-pied" in r.text and ".zone-nom" in r.text
            # Bug 3 — deep-link : ecoute hashchange + attente API + encodage.
            assert 'addEventListener("hashchange"' in r.text
            assert "apiPrete" in r.text
            assert "encoderChemin" in r.text and "decoderChemin" in r.text
            # Bug 4 — cadrage natif borne, pseudo-elements filtres, restore officiel.
            assert "scrollToContent" in r.text and "fitToViewport" in r.text
            assert "maxZoom" in r.text and "cameraUpdate" in r.text
            assert "restaurerFn" in r.text

    _courir(_t())


def test_url_ouverture_editeur_prive_parametrable(environ, monkeypatch):
    """Deep-link : EXCALIDRAW_URL_EDITEUR prioritaire, repli historique sinon."""
    ctx = ContexteLocal(upstream="http://127.0.0.1:9", base_ouverture="https://biblio.example.test")
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur")
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/schema.excalidraw"
    monkeypatch.delenv(NOM_ENV_URL_EDITEUR)
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "https://biblio.example.test/excalidraw/editeur#/rag/schema.excalidraw"


def test_url_ouverture_robuste_echappements_2026_09_17(environ, monkeypatch):
    """Non-regression ChatGPT E2E : sequence ``\\n`` litterale en fin d'env."""
    ctx = ContexteLocal(upstream="http://127.0.0.1:9", base_ouverture="https://biblio.example.test")
    # Cas reel constate en prod : `.../editeurn#/...` (backslash-n residuel).
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur\\n")
    assert url_ouverture(ctx, "ia/dessin-20260917-213431-2be091.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/ia/dessin-20260917-213431-2be091.excalidraw"
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur\\r\\n")
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/schema.excalidraw"
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur/ ")
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/schema.excalidraw"


def test_biblio_ecriture_refusee_en_lecture(environ):
    async def _t():
        serveur, url, sock, recus = _serveur_stub()
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(JETON_LECTURE, SCOPES_LECTURE) as c:
                ent = _entetes(JETON_LECTURE)
                r = await c.post("/mcp", content=_json_rpc("initialize", 1), headers=ent)
                session = r.headers["mcp-session-id"]
                r = await c.post("/mcp", content=_json_rpc(
                    "tools/call", 5, {"name": "library_save",
                                      "arguments": {"chemin": "x.excalidraw",
                                                    "elements": json.dumps(ELEMENTS_DEMO)}}),
                    headers={**ent, "mcp-session-id": session})
                corps = r.json()
                assert "error" in corps and corps["error"]["code"] == -32000
                assert recus == []
        finally:
            serveur.should_exit = True
            sock.close()

    _courir(_t())


# --- API HTTP --------------------------------------------------------------------------------

def _h_biblio() -> dict[str, str]:
    return {"Authorization": f"Bearer {JETON_BIBLIO}"}


def test_api_http_jeton_requis_et_roundtrip(environ):
    async def _t():
        async with _client(JETON_ECRITURE, SCOPES_ECRITURE) as c:
            r = await c.get("/bibliotheque", follow_redirects=False)
            assert r.status_code in (301, 302) and r.headers["location"] == "/editeur"
            r = await c.get("/editeur")
            assert r.status_code == 200 and "renderTopRightUI" in r.text
            r = await c.get("/api/liste")
            assert r.status_code == 401
            r = await c.get("/api/liste", headers=_h_biblio())
            assert r.status_code == 200 and r.json()["dossier"] == "Excalidraw"
            r = await c.post("/api/dossiers", json={"dossier": "rag"}, headers=_h_biblio())
            assert r.status_code == 200
            doc = {"type": "excalidraw", "version": 2, "elements": ELEMENTS_DEMO,
                   "appState": {}, "files": {}}
            r = await c.put("/api/document?chemin=rag/http.excalidraw", json=doc,
                            headers=_h_biblio())
            assert r.status_code == 200, r.text
            r = await c.get("/api/document?chemin=rag/http.excalidraw", headers=_h_biblio())
            _assert_rectangle_restaure(r.json()["document"]["elements"])
            r = await c.get("/api/document?chemin=../fuite.excalidraw", headers=_h_biblio())
            assert r.status_code == 400
            r = await c.put("/api/document?chemin=rag/mauvais.excalidraw",
                            json={"type": "x"}, headers=_h_biblio())
            assert r.status_code == 400

    _courir(_t())


# --- fidelite labels (excalidraw-mcp#22, E2E ChatGPT 2026-09-17) -------------------------------

def test_label_fleche_expande_au_milieu(environ):
    """Une fleche `label:{text}` devient un texte lie au milieu, sans shorthand."""
    els = [{"type": "arrow", "id": "a1", "x": 340, "y": 240, "width": 130, "height": 0,
            "points": [[0, 0], [130, 0]], "endArrowhead": "arrow",
            "label": {"text": "test", "fontSize": 16}}]
    out = bibliotheque.restaurer_elements(els)
    assert len(out) == 2
    fleche, texte = out
    assert "label" not in fleche
    assert fleche["boundElements"] == [{"id": "a1_label", "type": "text"}]
    assert texte["containerId"] == "a1" and texte["text"] == "test"
    # Milieu geometrique (405, 240) moins demi-taille estimee.
    assert abs(texte["x"] - (405 - texte["width"] / 2)) < 1e-6
    assert abs(texte["y"] - (240 - texte["height"] / 2)) < 1e-6


def test_label_idempotent_sans_doublon(environ):
    """Re-restaurer un document deja officiel ne duplique rien."""
    doc = bibliotheque.construire_document(ELEMENTS_DEMO)
    assert sum(1 for e in doc["elements"] if e.get("type") == "text") == 1
    reexp = bibliotheque.restaurer_elements(doc["elements"])
    assert len(reexp) == len(doc["elements"]) == 2
    assert all("label" not in e for e in reexp)


def test_label_residuel_sur_officiel_retire_sans_doublon(environ):
    """Shorthand residuel sur document deja officiel : retire, pas de doublon."""
    doc = bibliotheque.construire_document(ELEMENTS_DEMO)
    doc["elements"][0]["label"] = {"text": "Autre", "fontSize": 20}
    out = bibliotheque.restaurer_elements(doc["elements"])
    assert len(out) == 2
    assert all("label" not in e for e in out)
    assert sum(1 for e in out if e.get("type") == "text") == 1


def test_preuves_e2e_chatgpt_fidelite(environ):
    """Les deux preuves E2E doivent rendre tous leurs textes apres expansion."""
    for premier, second, etiquette in [
        ("Post-fix URL", "Second box", "test"),
        ("ChatGPT E2E", "Excalidraw OK", "deep-link"),
    ]:
        els = [
            {"type": "rectangle", "id": "g", "x": 120, "y": 190, "width": 220, "height": 100,
             "label": {"text": premier, "fontSize": 22}},
            {"type": "rectangle", "id": "d", "x": 470, "y": 190, "width": 220, "height": 100,
             "label": {"text": second, "fontSize": 22}},
            {"type": "arrow", "id": "f", "x": 340, "y": 240, "width": 130, "height": 0,
             "points": [[0, 0], [130, 0]], "label": {"text": etiquette, "fontSize": 16}},
        ]
        out = bibliotheque.restaurer_elements(els)
        textes = {e["text"] for e in out if e.get("type") == "text"}
        assert textes == {premier, second, etiquette}
        assert all("label" not in e for e in out)
        for e in out:
            if e.get("type") == "text":
                assert e.get("containerId") in ("g", "d", "f")
