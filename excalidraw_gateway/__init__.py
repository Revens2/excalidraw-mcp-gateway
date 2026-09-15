"""Passerelle d'authentification du MCP Excalidraw (rendu de diagrammes).

Copie-adaptation du pattern `tasks-mcp` / `calendar-mcp-gateway` : serveur
d'autorisation OAuth 2.1 colocalise (SDK python `mcp`), page de consentement,
et proxy transparent vers l'upstream excalidraw-mcp officiel (127.0.0.1:8122)
sur `/mcp`. La passerelle n'est PAS un serveur MCP : elle valide le jeton puis
relaie le trafic Streamable HTTP tel quel. Pas de Bearer statique : OAuth seul.
"""

__version__ = "1.0.0"
