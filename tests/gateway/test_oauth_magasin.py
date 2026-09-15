"""Tests du magasin d'etat OAuth (etat.json / clients.json).

Proprietes verifiees :
- deux read-modify-write concurrents (threads puis processus) ne se perdent pas ;
- un JSON invalide ou une erreur de permission leve EtatOAuthCorrompu (fail-closed),
  le fichier n'est JAMAIS remplace par un etat vide ;
- fichier absent au premier demarrage -> etat vide initial (comportement prevu) ;
- un code d'autorisation reste one-shot ;
- un jeton expire ou revoque est refuse ;
- les fichiers d'etat sont ecrits en 0600.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from mcp.shared.auth import OAuthClientInformationFull

from excalidraw_gateway.oauth import EtatOAuthCorrompu, MagasinOAuth


def _magasin(tmp_path: Path, clients: Path | None = None) -> MagasinOAuth:
    """Magasin isole : etat.json ET clients.json vivent sous tmp_path (jamais la prod)."""
    return MagasinOAuth(repertoire=tmp_path, clients=clients or (tmp_path / "clients.json"))


def _client_brut(client_id: str, secret: str = "s" * 32) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=["https://client.example/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        scope="excalidraw:lecture",
    )


# --- Concurrence ----------------------------------------------------------------------
def test_deux_enregistrements_clients_concurrents_ne_se_perdent_pas(tmp_path):
    """Deux enregistrements simultanes (threads) : les deux clients sont presents."""
    magasin = _magasin(tmp_path)
    barriere = threading.Barrier(2)

    def _enregistrer(client_id: str) -> None:
        barriere.wait()
        magasin.enregistrer_client(_client_brut(client_id))

    threads = [
        threading.Thread(target=_enregistrer, args=("client-a",)),
        threading.Thread(target=_enregistrer, args=("client-b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert magasin.client("client-a") is not None
    assert magasin.client("client-b") is not None


def test_deux_emissions_simultanees_ne_se_perdent_pas(tmp_path):
    """Deux poser_jetons simultanes (threads) : les deux paires sont presentes."""
    magasin = _magasin(tmp_path)
    barriere = threading.Barrier(2)

    def _emettre(jeton: str) -> None:
        barriere.wait()
        magasin.poser_jetons(
            {"jeton": jeton, "client_id": "c", "scopes": ["excalidraw:lecture"], "expire_a": int(time.time()) + 3600},
            {"jeton": jeton + "-r", "client_id": "c", "scopes": ["excalidraw:lecture"], "expire_a": int(time.time()) + 3600},
        )

    threads = [threading.Thread(target=_emettre, args=(j,)) for j in ("j-a", "j-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert magasin.lire_acces("j-a") is not None
    assert magasin.lire_acces("j-b") is not None
    assert magasin.lire_rafraichissement("j-a-r") is not None
    assert magasin.lire_rafraichissement("j-b-r") is not None


def test_emission_et_revocation_concurrentes_coherentes(tmp_path):
    """Une revocation et une emission simultanees : l'etat final contient bien les
    deux effets (le jeton revoque absent, le nouveau present)."""
    magasin = _magasin(tmp_path)
    magasin.poser_jetons(
        {"jeton": "j-ancien", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        {"jeton": "j-ancien-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
    )
    barriere = threading.Barrier(2)

    def _revoquer() -> None:
        barriere.wait()
        magasin.revoquer("j-ancien")

    def _emettre() -> None:
        barriere.wait()
        magasin.poser_jetons(
            {"jeton": "j-nouveau", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
            {"jeton": "j-nouveau-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        )

    threads = [threading.Thread(target=_revoquer), threading.Thread(target=_emettre)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert magasin.lire_acces("j-ancien") is None  # revoque
    assert magasin.lire_acces("j-nouveau") is not None  # emis, non perdu
    assert magasin.lire_rafraichissement("j-nouveau-r") is not None


def _processus_emission(tmp_path: str, jeton: str, barriere) -> None:
    """Worker multi-process : attend la barriere puis emet une paire de jetons."""
    magasin = MagasinOAuth(repertoire=Path(tmp_path))
    barriere.wait()
    magasin.poser_jetons(
        {"jeton": jeton, "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        {"jeton": jeton + "-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
    )


def test_deux_processus_concurrents_ne_se_perdent_pas(tmp_path):
    """Preuve inter-processus (flock) : deux processus qui emettent en meme temps
    ne s'ecrasent pas l'un l'autre."""
    contexte = multiprocessing.get_context("fork")
    barriere = contexte.Barrier(2)
    p1 = contexte.Process(target=_processus_emission, args=(str(tmp_path), "j-p1", barriere))
    p2 = contexte.Process(target=_processus_emission, args=(str(tmp_path), "j-p2", barriere))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0 and p2.exitcode == 0

    magasin = _magasin(tmp_path)
    assert magasin.lire_acces("j-p1") is not None
    assert magasin.lire_acces("j-p2") is not None


# --- Corruption / permissions : fail-closed -------------------------------------------
def test_json_invalide_leve_erreur_controlee(tmp_path):
    """Un etat.json malforme leve EtatOAuthCorrompu (jamais un {} silencieux)."""
    fichier = tmp_path / "etat.json"
    fichier.write_text("{pas du json", encoding="utf-8")
    magasin = _magasin(tmp_path)
    with pytest.raises(EtatOAuthCorrompu):
        magasin.lire_acces("nimporte-quel-jeton")


def test_corruption_du_registre_clients_n_est_pas_ecrasee(tmp_path):
    """Un clients.json corrompu fait echouer l'enregistrement SANS ecraser le fichier."""
    fichier = tmp_path / "clients.json"
    fichier.write_text("{corrompu", encoding="utf-8")
    magasin = _magasin(tmp_path, clients=fichier)
    with pytest.raises(EtatOAuthCorrompu):
        magasin.enregistrer_client(_client_brut("client-x"))
    # le contenu corrompu est preserve : aucune perte silencieuse
    assert fichier.read_text(encoding="utf-8") == "{corrompu"


def test_etat_illisible_par_permission_nefface_rien(tmp_path):
    """Un etat illisible (permission) fait echouer l'ecriture sans rien effacer."""
    fichier = tmp_path / "etat.json"
    fichier.write_text("{}", encoding="utf-8")
    os.chmod(fichier, 0)
    magasin = _magasin(tmp_path)
    with pytest.raises(EtatOAuthCorrompu):
        magasin.poser_jetons(
            {"jeton": "j", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
            {"jeton": "j-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        )
    os.chmod(fichier, 0o600)
    assert fichier.read_text(encoding="utf-8") == "{}"


# --- Comportements nominaux ------------------------------------------------------------
def test_fichier_absent_premier_demarrage(tmp_path):
    """ENOENT initial : l'etat vide est le comportement prevu (pas une erreur)."""
    magasin = _magasin(tmp_path)
    assert magasin.lire_acces("x") is None
    assert magasin.client("y") is None


def test_code_autorisation_one_shot(tmp_path):
    """Un code retire ne peut pas etre relu : pas de rejeu."""
    magasin = _magasin(tmp_path)
    magasin.poser_code("code-1", {"client_id": "c", "scopes": ["excalidraw:lecture"], "expire_a": int(time.time()) + 90})
    assert magasin.lire_code("code-1") is not None
    magasin.retirer_code("code-1")
    assert magasin.lire_code("code-1") is None


def test_jeton_expire_refuse(tmp_path):
    magasin = _magasin(tmp_path)
    passe = int(time.time()) - 10
    magasin.poser_jetons(
        {"jeton": "j-exp", "client_id": "c", "scopes": [], "expire_a": passe},
        {"jeton": "j-exp-r", "client_id": "c", "scopes": [], "expire_a": passe},
    )
    assert magasin.lire_acces("j-exp") is None  # expire


def test_jeton_revoque_refuse(tmp_path):
    magasin = _magasin(tmp_path)
    magasin.poser_jetons(
        {"jeton": "j-rev", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        {"jeton": "j-rev-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
    )
    assert magasin.lire_acces("j-rev") is not None
    magasin.revoquer("j-rev")
    assert magasin.lire_acces("j-rev") is None
    # la revocation d'un refresh token retire le refresh ; un access revoque
    # individuellement ne retire pas le refresh associe (clé distincte)
    magasin.revoquer("j-rev-r")
    assert magasin.lire_rafraichissement("j-rev-r") is None


def test_fichiers_ecrits_en_0600(tmp_path):
    magasin = _magasin(tmp_path)
    magasin.poser_jetons(
        {"jeton": "j", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
        {"jeton": "j-r", "client_id": "c", "scopes": [], "expire_a": int(time.time()) + 3600},
    )
    for nom in ("etat.json", "clients.json"):
        chemin = tmp_path / nom
        if chemin.exists():
            assert stat.S_IMODE(chemin.stat().st_mode) == 0o600, nom
