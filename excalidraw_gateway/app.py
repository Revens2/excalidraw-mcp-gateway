"""Assemblage de l'application ASGI de la passerelle Excalidraw.

Composition calquee sur ce que fait le SDK `mcp` pour le vault (adr/0015) :
- routes du serveur d'autorisation (`/authorize`, `/token`, `/register`, `/revoke`,
  metadonnees RFC 8414) via `mcp.server.auth.routes` ;
- metadonnees de ressource protegee RFC 9728 (les connecteurs y lisent les portees) ;
- page `/consentement` (phrase de passe) ;
- `/mcp` : middleware d'authentification (OAuth + Bearer statique) puis proxy transparent
  vers le conteneur upstream.

Tout le reste repond 404 : la passerelle n'expose que ce qui doit l'etre.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from excalidraw_gateway import bibliotheque
from excalidraw_gateway.bibliotheque import (
    TAILLE_MAX_DOC_OCTETS,
    ErreurBibliotheque,
)
from excalidraw_gateway.consentement import routes_consentement
from excalidraw_gateway.oauth import PORTEE, PORTEES, FournisseurOAuth, MagasinOAuth
from excalidraw_gateway.politique import PolitiqueOutils
from excalidraw_gateway.upstream import ProxyMCP

PORT_PAR_DEFAUT = 8123
CHEMIN_MCP = "/mcp"
# Politique d'autorisation explicite : lecture/ecriture par outil, inconnu -> fail-closed.
POLITIQUE = PolitiqueOutils()


def _config() -> tuple[str, str, int, str, str]:
    """(emetteur, upstream, port, jeton statique, repertoire oauth)."""

    def _requise(nom: str) -> str:
        valeur = os.environ.get(nom, "").strip().rstrip("/")
        if not valeur:
            raise RuntimeError(f"{nom} absent de l'environnement")
        return valeur

    emetteur = _requise("EXCALIDRAW_MCP_ISSUER")
    if not emetteur.startswith("https://"):
        raise RuntimeError("EXCALIDRAW_MCP_ISSUER doit etre en HTTPS")
    upstream = _requise("EXCALIDRAW_MCP_UPSTREAM")
    if not upstream.startswith("http://"):
        raise RuntimeError("EXCALIDRAW_MCP_UPSTREAM doit etre en HTTP (boucle locale)")
    port = int(os.environ.get("EXCALIDRAW_MCP_PORT", str(PORT_PAR_DEFAUT)))
    jeton = os.environ.get("EXCALIDRAW_MCP_TOKEN", "")
    if jeton and len(jeton) < 32:
        raise RuntimeError("EXCALIDRAW_MCP_TOKEN trop court : 32 caracteres minimum")
    return emetteur, upstream, port, jeton, os.environ.get("EXCALIDRAW_MCP_OAUTH_DIR", "")


_journal_biblio = logging.getLogger("uvicorn.error")
_PAGE_BIBLIOTHEQUE = Path(__file__).resolve().parent / "statique" / "bibliotheque.html"
_PAGE_EDITEUR = Path(__file__).resolve().parent / "statique" / "editeur.html"


async def _page_bibliotheque(_: Request) -> HTMLResponse:
    """Page bibliotheque distante (publique ; le jeton reste cote navigateur)."""
    try:
        html = _PAGE_BIBLIOTHEQUE.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("bibliotheque indisponible", status_code=500)  # type: ignore[return-value]
    return HTMLResponse(html)


async def _page_editeur(_: Request) -> HTMLResponse:
    """Editeur principal integre (distant + local, publique ; voir /bibliotheque)."""
    try:
        html = _PAGE_EDITEUR.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("editeur indisponible", status_code=500)  # type: ignore[return-value]
    return HTMLResponse(html)


def _jeton_biblio_ok(request: Request) -> bool:
    """Bearer EXCALIDRAW_BIBLIO_TOKEN (comparaison constante). Fail-closed."""
    attendu = os.environ.get("EXCALIDRAW_BIBLIO_TOKEN", "")
    if len(attendu) < 32:
        return False
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() != "bearer ":
        return False
    return hmac.compare_digest(auth[7:].strip(), attendu)


def _refus_biblio() -> JSONResponse:
    if len(os.environ.get("EXCALIDRAW_BIBLIO_TOKEN", "")) < 32:
        return JSONResponse(
            {"ok": False, "erreur": "bibliotheque non configuree (jeton absent)"},
            status_code=503,
        )
    return JSONResponse(
        {"ok": False, "erreur": "jeton bibliotheque requis (Authorization: Bearer)"},
        status_code=401,
    )


def _reponse_biblio(fn):
    """Execute `fn() -> dict` ; ErreurBibliotheque -> 400, inattendu -> 500."""
    try:
        return JSONResponse({"ok": True, **fn()})
    except ErreurBibliotheque as exc:
        return JSONResponse({"ok": False, "erreur": str(exc)}, status_code=400)
    except Exception as exc:  # garde-fou : jamais de fuite de pile ni de secret
        _journal_biblio.warning("api bibliotheque en echec: %s", exc.__class__.__name__)
        return JSONResponse({"ok": False, "erreur": "echec interne"}, status_code=500)


async def _api_liste(request: Request) -> JSONResponse:
    if not _jeton_biblio_ok(request):
        return _refus_biblio()
    chemin = request.query_params.get("chemin", "")
    return _reponse_biblio(lambda: bibliotheque.lister(chemin))


async def _api_dossiers(request: Request) -> JSONResponse:
    if not _jeton_biblio_ok(request):
        return _refus_biblio()
    try:
        corps = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"ok": False, "erreur": "corps JSON invalide"}, status_code=400)
    dossier = corps.get("dossier") if isinstance(corps, dict) else None
    if not isinstance(dossier, str):
        return JSONResponse({"ok": False, "erreur": "dossier requis"}, status_code=400)
    return _reponse_biblio(lambda: bibliotheque.creer_dossier(dossier))


async def _api_document(request: Request) -> JSONResponse:
    if not _jeton_biblio_ok(request):
        return _refus_biblio()
    chemin = request.query_params.get("chemin", "")
    if request.method == "GET":
        return _reponse_biblio(lambda: bibliotheque.charger(chemin))
    # PUT : corps = document .excalidraw JSON.
    brut = await request.body()
    if len(brut) > TAILLE_MAX_DOC_OCTETS + 1024 * 1024:
        return JSONResponse({"ok": False, "erreur": "document trop volumineux"}, status_code=413)
    try:
        document = json.loads(brut or b"null")
    except ValueError:
        return JSONResponse({"ok": False, "erreur": "corps JSON invalide"}, status_code=400)
    if not isinstance(document, dict):
        return JSONResponse({"ok": False, "erreur": "document objet attendu"}, status_code=400)
    return _reponse_biblio(lambda: bibliotheque.enregistrer(chemin, document))


async def _api_deplacer(request: Request) -> JSONResponse:
    if not _jeton_biblio_ok(request):
        return _refus_biblio()
    try:
        corps = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"ok": False, "erreur": "corps JSON invalide"}, status_code=400)
    if not isinstance(corps, dict) or not isinstance(corps.get("source"), str) or not isinstance(
        corps.get("destination"), str
    ):
        return JSONResponse(
            {"ok": False, "erreur": "source et destination requises"}, status_code=400
        )
    return _reponse_biblio(lambda: bibliotheque.deplacer(corps["source"], corps["destination"]))


def _sante(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "excalidraw-mcp-gateway",
            "mcp": CHEMIN_MCP,
        }
    )


def _defaut(_: Request) -> PlainTextResponse:
    return PlainTextResponse("Not Found", status_code=404)


def construire_application(
    emetteur: str | None = None,
    upstream: str | None = None,
    jeton_statique: str | None = None,
    repertoire_oauth: str | None = None,
) -> Starlette:
    """Construit l'application. Les parametres remplacent l'environnement (tests)."""
    emetteur_reel, upstream_reel, _, jeton_reel, oauth_reel = _config()
    emetteur = (emetteur or emetteur_reel).rstrip("/")
    upstream = upstream or upstream_reel
    jeton = jeton_statique if jeton_statique is not None else jeton_reel

    fournisseur = FournisseurOAuth(
        emetteur,
        magasin=MagasinOAuth(repertoire=repertoire_oauth) if repertoire_oauth else None,
        jeton_statique=jeton,
    )
    base_ouverture = emetteur.split("/oauth")[0] if "/oauth" in emetteur else emetteur
    proxy = ProxyMCP(upstream, politique=POLITIQUE, base_ouverture=base_ouverture)

    # Spec strict: resource = https://mymcps.duckdns.org/{service}/mcp, issuer = https://mymcps.duckdns.org/oauth/{service}
    resource_url_str = emetteur.replace("/oauth", "") + CHEMIN_MCP if "/oauth" in emetteur else f"{emetteur}{CHEMIN_MCP}"
    routes: list[Route] = [
        *create_auth_routes(
            fournisseur,
            issuer_url=AnyHttpUrl(emetteur),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=PORTEES,
                default_scopes=[PORTEE],
            ),
        ),
        *create_protected_resource_routes(
            resource_url=AnyHttpUrl(resource_url_str),
            authorization_servers=[AnyHttpUrl(emetteur)],
            scopes_supported=PORTEES,
            resource_name="Excalidraw MCP (passerelle)",
        ),
        *routes_consentement(fournisseur),
        Route("/health", _sante, methods=["GET"]),
        Route(
            CHEMIN_MCP,
            endpoint=RequireAuthMiddleware(
                proxy,
                required_scopes=[PORTEE],
                resource_metadata_url=build_resource_metadata_url(AnyHttpUrl(resource_url_str)),
            ),
            methods=["GET", "POST", "DELETE", "OPTIONS"],
        ),
        # Bibliotheque distante : page + API fichiers (jeton Bearer, voir EXCALIDRAW_BIBLIO_TOKEN).
        Route("/bibliotheque", _page_bibliotheque, methods=["GET"]),
        # Editeur principal integre : distant + local (meme API, deep-link #/<chemin>).
        Route("/editeur", _page_editeur, methods=["GET"]),
        Route("/api/liste", _api_liste, methods=["GET"]),
        Route("/api/document", _api_document, methods=["GET", "PUT"]),
        Route("/api/dossiers", _api_dossiers, methods=["POST"]),
        Route("/api/deplacer", _api_deplacer, methods=["POST"]),
        # Fourre-tout : la passerelle n'expose que ce qui precede.
        Route("/{chemin:path}", _defaut, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"]),
    ]

    application = Starlette(routes=routes)
    # L'AuthenticationMiddleware peuplie scope["user"]/scope["auth"] sur toutes les
    # requetes ; RequireAuthMiddleware (sur /mcp) refuse ensuite sans jeton valide.
    # Le controle fin lecture/ecriture par outil est applique dans ProxyMCP (politique),
    # sur la base des portees du jeton valide, jamais d'un en-tete client.
    return AuthenticationMiddleware(
        application,
        backend=BearerAuthBackend(ProviderTokenVerifier(fournisseur)),
    )
