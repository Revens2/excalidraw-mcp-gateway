"""Ere protocolaire et durcissements du relais (lot MCP v2, 2026-09-10).

- `classer_ere` : matrice en-tete x corps (_meta protocolVersion) x methode ;
- relais reel vers un upstream d'enregistrement : en-tetes 2026-07-28 transmis sans
  reecriture, quirk ChatGPT rabaisse, doublons / methodes inconnues / enveloppe
  moderne sous en-tete historique refuses SANS contacter l'upstream ;
- filtrage tools/list : forme 2026-07-28 (resultType conserve, cache prive), SSE
  multi-lignes / sans espace / CRLF, reponse illisible jamais relayee.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from excalidraw_gateway.app import construire_application
from excalidraw_gateway.oauth import hacher_phrase
from excalidraw_gateway.politique import OUTILS_ECRITURE, OUTILS_LECTURE
from excalidraw_gateway.upstream import classer_ere

EMETTEUR = "https://tasks.example.test"
JETON_LECTURE = "l" * 40
JETON_ECRITURE = "e" * 40
MODERNE = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERNE,
    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


# --- classer_ere (pur) -----------------------------------------------------------------


def _rpc(methode, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params or {}}


@pytest.mark.parametrize(
    ("entete", "donnees", "attendu"),
    [
        # quirk ChatGPT : en-tete moderne, corps historique -> rabaisse
        (MODERNE, _rpc("tools/list"), ("2025-11-25", None, True)),
        (MODERNE, _rpc("initialize", {"protocolVersion": "2025-06-18"}), ("2025-11-25", None, True)),
        (MODERNE, _rpc("tools/call", {"name": "read_me"}), ("2025-11-25", None, True)),
        (MODERNE, {"jsonrpc": "2.0", "method": "notifications/initialized"}, ("2025-11-25", None, True)),
        (MODERNE, {"jsonrpc": "2.0", "id": 3, "result": {}}, ("2025-11-25", None, True)),
        (MODERNE, None, ("2025-11-25", None, True)),  # GET flux / DELETE
        (" 2026-07-28 ", _rpc("ping"), ("2025-11-25", None, True)),
        # _meta non versionne (progressToken) : reste historique
        (MODERNE, _rpc("tools/call", {"name": "x", "_meta": {"progressToken": 1}}), ("2025-11-25", None, True)),
        # enveloppe moderne : jamais rabaissee
        (MODERNE, _rpc("tools/list", {"_meta": META}), (MODERNE, None, False)),
        (MODERNE, _rpc("server/discover", {"_meta": META}), (MODERNE, None, False)),
        (
            MODERNE,
            _rpc("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": None}}),
            (MODERNE, None, False),
        ),
        # methode moderne sans enveloppe : intacte (l'upstream refuse), jamais rabaissee
        (MODERNE, _rpc("server/discover"), (MODERNE, None, False)),
        # historique / absent / inconnu sans enveloppe : tel quel
        ("2025-06-18", _rpc("tools/list"), ("2025-06-18", None, False)),
        (None, _rpc("initialize"), (None, None, False)),
        ("2099-01-01", _rpc("tools/list"), ("2099-01-01", None, False)),
    ],
)
def test_classer_ere_matrice(entete, donnees, attendu):
    assert classer_ere(entete, donnees) == attendu


@pytest.mark.parametrize("entete", [None, "2025-06-18", "2025-11-25", "2024-11-05"])
def test_classer_ere_enveloppe_moderne_sous_entete_historique_refusee(entete):
    version, refus, rabaissee = classer_ere(entete, _rpc("tools/call", {"name": "read_me", "_meta": META}))
    assert refus and not rabaissee


# --- relais reel -----------------------------------------------------------------------


@pytest.fixture()
def environ(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCALIDRAW_MCP_ISSUER", EMETTEUR)
    monkeypatch.setenv("EXCALIDRAW_MCP_UPSTREAM", "http://127.0.0.1:9")
    monkeypatch.setenv("EXCALIDRAW_MCP_OAUTH_DIR", str(tmp_path))
    monkeypatch.setenv("EXCALIDRAW_MCP_TOKEN", JETON_ECRITURE)
    monkeypatch.setenv("EXCALIDRAW_MCP_CONSENT_HASH", hacher_phrase("phrase-de-test-2026"))
    return tmp_path


def _stub(reponse_liste=None):
    """Upstream d'enregistrement : memorise en-tetes + corps, repond selon la methode."""
    recus: list[dict] = []

    async def _post(requete: Request) -> Response:
        corps = await requete.body()
        donnees = json.loads(corps) if corps else None
        recus.append({"headers": dict(requete.headers), "body": donnees})
        methode = donnees.get("method") if isinstance(donnees, dict) else None
        if methode == "tools/list" and reponse_liste is not None:
            contenu, type_ = reponse_liste
            return Response(contenu, media_type=type_)
        resultat = {"jsonrpc": "2.0", "id": (donnees or {}).get("id"), "result": {"ok": True}}
        return Response(json.dumps(resultat), media_type="application/json")

    app = Starlette(routes=[Route("/mcp", _post, methods=["POST", "GET", "DELETE"])])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    serveur = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    threading.Thread(target=serveur.run, kwargs={"sockets": [sock]}, daemon=True).start()
    for _ in range(300):
        if serveur.started:
            break
        time.sleep(0.02)
    return serveur, f"http://127.0.0.1:{sock.getsockname()[1]}", sock, recus


def _client(jeton, portees):
    os.environ["EXCALIDRAW_MCP_TOKEN_SCOPES"] = portees
    app = construire_application(jeton_statique=jeton)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=EMETTEUR)


def _base(jeton, **extra):
    return {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": f"Bearer {jeton}",
        **extra,
    }


def _executer(scenario, reponse_liste=None, jeton=JETON_ECRITURE,
              portees="excalidraw:lecture excalidraw:ecriture"):
    async def _t():
        serveur, url, sock, recus = _stub(reponse_liste)
        try:
            os.environ["EXCALIDRAW_MCP_UPSTREAM"] = url
            async with _client(jeton, portees) as c:
                return await scenario(c, recus)
        finally:
            serveur.should_exit = True
            sock.close()

    return asyncio.run(_t())


def test_enveloppe_moderne_relayee_sans_reecriture(environ):
    async def scenario(c, recus):
        r = await c.post(
            "/mcp",
            content=json.dumps(_rpc("tools/call", {"name": "read_me", "arguments": {"uid": "u"}, "_meta": META})),
            headers=_base(JETON_ECRITURE, **{"mcp-protocol-version": MODERNE, "mcp-method": "tools/call",
                                            "mcp-name": "read_me", "mcp-param-uid": "u"}),
        )
        assert r.status_code == 200, r.text
        h = recus[0]["headers"]
        assert h["mcp-protocol-version"] == MODERNE
        assert h["mcp-method"] == "tools/call" and h["mcp-name"] == "read_me"
        assert h["mcp-param-uid"] == "u"

    _executer(scenario)


def test_quirk_chatgpt_rabaisse_vers_handshake(environ):
    async def scenario(c, recus):
        r = await c.post("/mcp", content=json.dumps(_rpc("tools/list")),
                         headers=_base(JETON_ECRITURE, **{"mcp-protocol-version": MODERNE}))
        assert r.status_code == 200
        assert recus[0]["headers"]["mcp-protocol-version"] == "2025-11-25"

    _executer(scenario)


def test_get_flux_rabaisse(environ):
    async def scenario(c, recus):
        r = await c.get("/mcp", headers=_base(JETON_ECRITURE, **{"mcp-protocol-version": MODERNE}))
        assert r.status_code == 200
        assert recus[0]["headers"]["mcp-protocol-version"] == "2025-11-25"

    _executer(scenario)


@pytest.mark.parametrize(
    ("entetes", "corps", "code"),
    [
        # enveloppe moderne sous en-tete historique
        ({"mcp-protocol-version": "2025-06-18"}, _rpc("tools/list", {"_meta": META}), -32020),
        ({}, _rpc("tools/list", {"_meta": META}), -32020),
        # methode inconnue
        ({}, _rpc("tasks/get"), -32601),
        ({}, {"jsonrpc": "2.0", "id": 1, "method": 42}, -32601),
    ],
)
def test_refus_locaux_sans_contact_upstream(environ, entetes, corps, code):
    async def scenario(c, recus):
        r = await c.post("/mcp", content=json.dumps(corps), headers=_base(JETON_ECRITURE, **entetes))
        assert r.json()["error"]["code"] == code
        assert recus == []

    _executer(scenario)


def test_cles_json_dupliquees_refusees(environ):
    brut = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_me","name":"create_view"}}'

    async def scenario(c, recus):
        r = await c.post("/mcp", content=brut, headers=_base(JETON_ECRITURE))
        assert r.json()["error"]["code"] == -32600
        assert recus == []

    _executer(scenario)


@pytest.mark.parametrize("nom", ["mcp-method", "mcp-protocol-version", "mcp-name", "mcp-param-uid"])
def test_entete_mcp_duplique_refuse(environ, nom):
    async def scenario(c, recus):
        entetes = [(k, v) for k, v in _base(JETON_ECRITURE).items()] + [(nom, "a"), (nom, "b")]
        r = await c.post("/mcp", content=json.dumps(_rpc("tools/list")), headers=entetes)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32020
        assert recus == []

    _executer(scenario)


def test_notification_inconnue_relayee(environ):
    async def scenario(c, recus):
        await c.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}),
                     headers=_base(JETON_ECRITURE))
        assert len(recus) == 1

    _executer(scenario)


@pytest.mark.parametrize("outil", sorted(OUTILS_ECRITURE))
def test_moderne_ecriture_refusee_en_lecture_meme_si_mcp_name_ment(environ, outil):
    """La politique lit le CORPS ; un Mcp-Name mensonger ne donne aucun droit."""

    async def scenario(c, recus):
        r = await c.post(
            "/mcp",
            content=json.dumps(_rpc("tools/call", {"name": outil, "arguments": {}, "_meta": META})),
            headers=_base(JETON_LECTURE, **{"mcp-protocol-version": MODERNE, "mcp-method": "tools/call",
                                           "mcp-name": "read_me"}),
        )
        assert r.json()["error"]["code"] == -32000
        assert recus == []

    _executer(scenario, jeton=JETON_LECTURE, portees="excalidraw:lecture")


def _liste_moderne():
    outils = [{"name": n} for n in sorted(OUTILS_LECTURE | OUTILS_ECRITURE)] + [{"name": "admin_x"}]
    return {"jsonrpc": "2.0", "id": 1, "result": {"tools": outils, "resultType": "complete",
                                                  "cacheScope": "public", "ttlMs": 60000}}


def test_liste_moderne_filtree_cache_prive(environ):
    async def scenario(c, recus):
        r = await c.post("/mcp", content=json.dumps(_rpc("tools/list", {"_meta": META})),
                         headers=_base(JETON_LECTURE, **{"mcp-protocol-version": MODERNE, "mcp-method": "tools/list"}))
        res = r.json()["result"]
        assert {t["name"] for t in res["tools"]} == set(OUTILS_LECTURE)
        assert res["resultType"] == "complete"
        assert res["cacheScope"] == "private" and res["ttlMs"] == 0

    _executer(scenario, reponse_liste=(json.dumps(_liste_moderne()), "application/json"),
              jeton=JETON_LECTURE, portees="excalidraw:lecture")


@pytest.mark.parametrize(
    "gabarit",
    [
        "event: message\r\ndata: {j}\r\n\r\n",  # CRLF
        "event: message\ndata:{j}\n\n",  # sans espace
        "id: 1\n{multi}\n\n",  # multi-lignes (une ligne data: par ligne JSON indentee)
    ],
)
def test_liste_sse_variantes_filtrees(environ, gabarit):
    texte = json.dumps(_liste_moderne())
    multi = "\n".join("data: " + ligne for ligne in json.dumps(_liste_moderne(), indent=1).splitlines())
    sse = gabarit.format(j=texte, multi=multi)

    async def scenario(c, recus):
        r = await c.post("/mcp", content=json.dumps(_rpc("tools/list")), headers=_base(JETON_LECTURE))
        donnees = [ligne for ligne in r.text.splitlines() if ligne.startswith("data:")]
        noms = set()
        for ligne in donnees:
            charge = json.loads(ligne[5:].strip())
            noms |= {t["name"] for t in charge["result"]["tools"]}
        assert noms == set(OUTILS_LECTURE)

    _executer(scenario, reponse_liste=(sse, "text/event-stream"), jeton=JETON_LECTURE, portees="excalidraw:lecture")


@pytest.mark.parametrize(("contenu", "type_"), [("data: {pas du json\n\n", "text/event-stream"),
                                                 ("{pas du json", "application/json"),
                                                 ("ok", "text/plain")])
def test_liste_illisible_jamais_relayee(environ, contenu, type_):
    async def scenario(c, recus):
        r = await c.post("/mcp", content=json.dumps(_rpc("tools/list")), headers=_base(JETON_LECTURE))
        assert r.json()["error"]["code"] == -32603
        assert "admin" not in r.text

    _executer(scenario, reponse_liste=(contenu, type_), jeton=JETON_LECTURE, portees="excalidraw:lecture")


def test_acteur_forge_par_client_jamais_relaye(environ):
    async def scenario(c, recus):
        await c.post("/mcp", content=json.dumps(_rpc("tools/list")),
                     headers=_base(JETON_ECRITURE, **{"x-tasks-mcp-acteur": "usurpateur", "x-tasks-mcp-mode": "cli"}))
        h = recus[0]["headers"]
        assert h.get("x-tasks-mcp-acteur") != "usurpateur"

    _executer(scenario)
