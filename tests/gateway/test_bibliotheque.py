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
    assert relu["document"]["elements"] == ELEMENTS_DEMO
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
                assert contenu["elements"] == ELEMENTS_DEMO
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
                # Fichier = elements resolus du checkpoint (r9), pas la requete brute.
                relu = bibliotheque.charger("rag/ia.excalidraw")
                assert relu["document"]["elements"] == _CHECKPOINT_ELEMENTS
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
                # Fichier = elements resolus du checkpoint (r9), pas la requete brute.
                relu = bibliotheque.charger(structure["fichier"].removeprefix("Excalidraw/"))
                assert relu["document"]["elements"] == _CHECKPOINT_ELEMENTS
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


def test_url_ouverture_editeur_prive_parametrable(environ, monkeypatch):
    """Deep-link : EXCALIDRAW_URL_EDITEUR prioritaire, repli historique sinon."""
    ctx = ContexteLocal(upstream="http://127.0.0.1:9", base_ouverture="https://biblio.example.test")
    monkeypatch.setenv(NOM_ENV_URL_EDITEUR, "http://10.200.114.203:8130/editeur")
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "http://10.200.114.203:8130/editeur#/rag/schema.excalidraw"
    monkeypatch.delenv(NOM_ENV_URL_EDITEUR)
    assert url_ouverture(ctx, "rag/schema.excalidraw") == \
        "https://biblio.example.test/excalidraw/editeur#/rag/schema.excalidraw"


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
            assert r.json()["document"]["elements"] == ELEMENTS_DEMO
            r = await c.get("/api/document?chemin=../fuite.excalidraw", headers=_h_biblio())
            assert r.status_code == 400
            r = await c.put("/api/document?chemin=rag/mauvais.excalidraw",
                            json={"type": "x"}, headers=_h_biblio())
            assert r.status_code == 400

    _courir(_t())
