"""Phase 3c MCP adapter (ADR-028): a stdio MCP server that is only a client of the local REST API
(D-Q4). It never opens SQLite and holds no business rules. ``server.py`` is the only module that
imports the optional ``mcp`` SDK (install with ``ame[mcp]``)."""
