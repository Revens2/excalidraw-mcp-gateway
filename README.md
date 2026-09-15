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

Deploiement : voir l'unite `excalidraw-gateway.service` et le vhost
`mymcps.duckdns.org` versionnes dans `Revens2/vps-etude-infra`. Aucun secret
dans ce depot.
