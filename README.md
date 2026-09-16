# excalidraw-mcp-gateway

Passerelle OAuth 2.1 + proxy pour le MCP Excalidraw officiel (`excalidraw/excalidraw-mcp`).

Copie-adaptation du pattern `tasks-mcp` / `calendar-mcp-gateway` : serveur
d'autorisation colocalise (SDK python `mcp` v2, PKCE/DCR, metadata RFC 8414/9728),
page `/consentement` (phrase de passe, seule l'empreinte PBKDF2 est stockee),
middleware `/mcp` avec politique d'autorisation outil par outil (fail-closed).

- Lecture (`excalidraw:lecture`) : `read_me`, `read_checkpoint`.
- Ecriture (`excalidraw:ecriture`) : `create_view`, `save_checkpoint`,
  `export_to_excalidraw` (televerse vers excalidraw.com : sortie de donnee).
- OAuth seul, pas de Bearer statique.

## Bibliotheque distante `Excalidraw` (persistance serveur)

Les dessins IA et humains sont persistés en `.excalidraw` standard sous
`/srv/excalidraw/data/bibliotheque` (racine logique `Excalidraw`, chemins
relatifs uniquement, `..`/liens symboliques/traversal refuses, ecriture
atomique). Fichiers re-ouvrables tels quels dans Excalidraw.

Outils MCP servis localement par la passerelle (jamais relayes a l'upstream) :

- Lecture : `library_list`, `library_load`.
- Ecriture : `library_mkdir`, `library_save` (`elements` JSON ou `checkpoint_id`),
  `library_move` (renommer/deplacer).
- `create_view` est systematiquement persiste : `enregistrer_sous`
  (ex. `rag/schema.excalidraw`) designe le chemin (prioritaire) ; sans lui,
  autosave automatique sous `Excalidraw/ia/` (nom horodate sans collision,
  confinement inchange). Reponse enrichie de `fichier` + `url_ouverture`
  (`.../excalidraw/editeur#/<chemin>`, editeur principal) + `persistance_ok`.
  Le rendu n'est jamais sacrifie : un echec de persistance est signale dans
  la reponse (`persistance_ok: False`), rendu preserve. Un `enregistrer_sous`
  invalide reste refuse avant tout relais (fail-closed, upstream jamais
  contacte pour une demande inexploitable).

Page web : `/editeur` (**editeur principal integre** : « Ouvrir distant » /
« Sauvegarder distant » / « Ouvrir local » / « Sauvegarder local », deep-link
`#/<chemin>`, meme API distante) + `/bibliotheque` conservee (navigation
dossiers, mkdir/deplacer, repli JSON) + API `/api/liste`, `/api/document`
(GET/PUT), `/api/dossiers`, `/api/deplacer`
(Jeton `EXCALIDRAW_BIBLIO_TOKEN` en `Authorization: Bearer`, jamais en URL).
Expose via nginx : `https://mymcps.duckdns.org/excalidraw/editeur` et
`http://145.241.171.189/` (instance principale ; frontend officiel conserve
tel quel en repli sur `http://145.241.171.189/officiel/`,
conteneur `excalidraw-front` inchange).

Deploiement : voir l'unite `excalidraw-gateway.service` et les vhosts
`mymcps.duckdns.org` / `excalidraw-ip` versionnes dans
`Revens2/vps-etude-infra`. Aucun secret dans ce depot (voir `.env.example`).
