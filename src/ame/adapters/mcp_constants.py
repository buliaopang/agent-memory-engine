"""Constants shared by the REST app and the optional MCP adapter (ADR-028).

Kept outside ``ame.adapters.mcp`` so the core server never imports the optional ``mcp`` SDK.
"""

from __future__ import annotations

MCP_USER_AGENT_PREFIX = "ame-mcp/"
# POST routes that only read (search / context). Allowed when MCP is read-only.
MCP_READ_POSTS = frozenset({"/api/v1/memories/search", "/api/v1/context"})
