"""Politique d'autorisation outil par outil de la passerelle Excalidraw.

La securite ne repose ni sur une heuristique de nom, ni sur la seule disparition
d'un outil de `tools/list` : chaque outil expose par l'upstream est classe
explicitement ci-dessous. Tout outil absent de cette classification est INCONNU :
jamais annonce dans `tools/list`, jamais executable (fail-closed) tant qu'un
mainteneur ne l'a pas classe et deploye.

Liste des outils constatee sur l'upstream excalidraw-mcp officiel deploye
(2026-09-15, 5 outils : read_me, create_view, export_to_excalidraw,
save_checkpoint, read_checkpoint).

Note : `export_to_excalidraw` televerse le diagramme vers excalidraw.com (URL
publique de partage) : c'est une sortie de donnee, donc classee en ecriture
(soumise au consentement humain avec la portee d'ecriture).
"""

from __future__ import annotations

from dataclasses import dataclass

from excalidraw_gateway.oauth import PORTEE, PORTEE_ECRITURE

# Lecture seule : aucune donnee n'est creee ni envoyee a l'exterieur.
OUTILS_LECTURE: frozenset[str] = frozenset(
    {
        "read_me",
        "read_checkpoint",
    }
)

# Mutateurs / sorties : rendu d'un diagramme, sauvegarde d'etat, partage public.
OUTILS_ECRITURE: frozenset[str] = frozenset(
    {
        "create_view",
        "save_checkpoint",
        "export_to_excalidraw",
    }
)

# Aucun outil d'administration cote excalidraw. Les outils inconnus sont de toute
# facon refuses (fail-closed) par `autoriser_call` ci-dessous.
OUTILS_ADMIN: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PolitiqueOutils:
    """Regle d'acces a un outil upstream selon les portees du jeton valide.

    - outil de lecture  -> portee lecture requise ;
    - outil d'ecriture  -> portee ecriture requise ;
    - outil d'administration -> toujours refuse pour un client externe ;
    - tout autre nom (inconnu / pas encore classe) -> refuse (fail-closed),
      meme pour un jeton portant toutes les portees connues.
    """

    portee_lecture: str = PORTEE
    portee_ecriture: str = PORTEE_ECRITURE
    lecture: frozenset[str] = OUTILS_LECTURE
    ecriture: frozenset[str] = OUTILS_ECRITURE
    admin: frozenset[str] = OUTILS_ADMIN

    def connus(self) -> frozenset[str]:
        """Ensemble des outils classes (lecture + ecriture + admin)."""
        return self.lecture | self.ecriture | self.admin

    def visibles(self, portees: set[str]) -> set[str]:
        """Outils annoncables a un jeton portant `portees` (filtre de tools/list).

        Les outils d'administration et les outils inconnus ne sont jamais
        annonces, quel que soit le jeton.
        """
        if self.portee_lecture not in portees:
            # Cas theorique : le middleware exige deja la portee lecture pour
            # atteindre /mcp. Par defaut de robustesse : aucun outil annonce.
            return set()
        visibles = set(self.lecture)
        if self.portee_ecriture in portees:
            visibles |= set(self.ecriture)
        return visibles

    def autoriser_call(self, nom: str, portees: set[str]) -> str | None:
        """Raison de refus d'un `tools/call`, ou ``None`` si l'appel est autorise.

        Appelee AVANT tout envoi vers l'upstream.
        """
        if nom in self.admin:
            return "outil d'administration interdit aux clients de la passerelle"
        if nom in self.ecriture:
            if self.portee_ecriture in portees:
                return None
            return f"portee {self.portee_ecriture} requise pour cet outil"
        if nom in self.lecture:
            if self.portee_lecture in portees:
                return None
            return f"portee {self.portee_lecture} requise pour cet outil"
        return "outil inconnu ou non classe : appel refuse (fail-closed)"
