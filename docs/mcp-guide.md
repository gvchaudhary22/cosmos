# MCP Server Integration

COSMOS can be exposed as an MCP (Model Context Protocol) server, allowing any MCP-compatible client to query the COSMOS knowledge base and execute COSMOS actions.

---

## What COSMOS Exposes via MCP

### Resources
- `cosmos://kb/{entity_id}` — retrieve a specific KB document
- `cosmos://health` — service health status
- `cosmos://eval/latest` — latest eval report

### Tools
| Tool | Description | Auth required |
|------|-------------|---------------|
| `cosmos_search` | Search KB with natural language query | Yes |
| `cosmos_hybrid_chat` | Full hybrid chat (KB + code + DB tiers) | Yes |
| `cosmos_get_entity` | Get specific KB entity by ID | Yes |
| `cosmos_get_schema` | Get DB schema for a table | Yes |
| `cosmos_get_endpoint` | Get API contract for an endpoint | Yes |
| `cosmos_get_action` | Get action contract (P6) | Yes |
| `cosmos_get_workflow` | Get workflow runbook (P7) | Yes |

---

## MCP Server Configuration

COSMOS's MCP server is served at `/cosmos/mcp` on the main FastAPI process.

### Claude Code integration
Add to `.claude/settings.json` (or `~/.claude/settings.json` for global):
```json
{
  "mcpServers": {
    "cosmos": {
      "url": "http://localhost:10001/cosmos/mcp",
      "headers": {
        "X-Company-ID": "your_company_id",
        "Authorization": "Bearer your_mars_jwt"
      }
    }
  }
}
```

After adding, restart your Claude Code session. COSMOS tools appear in the tool panel.

### Cursor / VS Code integration
```json
// .cursor/mcp.json or .vscode/mcp.json
{
  "cosmos": {
    "url": "http://localhost:10001/cosmos/mcp",
    "auth": {
      "type": "bearer",
      "token": "your_mars_jwt"
    }
  }
}
```

---

## Tool Usage Examples

### Search the knowledge base
```
cosmos_search({
  "query": "how do I cancel an order?",
  "company_id": "42",
  "top_k": 5
})
```

Response:
```json
{
  "results": [
    {
      "entity_id": "pillar_6_action_contracts/cancel_order/index",
      "content": "...",
      "trust_score": 0.9,
      "pillar": "P6",
      "score": 0.94
    }
  ],
  "query_mode": "act",
  "confidence": 0.87
}
```

### Full hybrid chat
```
cosmos_hybrid_chat({
  "message": "Why is order 5001234 stuck in pending pickup?",
  "company_id": "42",
  "session_id": "sess_abc123"
})
```

Response:
```json
{
  "response": "Order 5001234 is stuck in pending pickup because... [1] [2]",
  "citations": [
    {"id": 1, "entity_id": "...", "pillar": "P7"},
    {"id": 2, "entity_id": "...", "pillar": "P1"}
  ],
  "confidence": 0.82,
  "wave_trace_id": "wt_xyz789"
}
```

---

## Authentication

All MCP tool calls require:
1. `company_id` — Shiprocket tenant identifier (enforces tenant isolation)
2. MARS JWT in Authorization header (validated by COSMOS → MARS)

If auth fails: COSMOS returns `{"error": "unauthorized", "code": "ERR-COSMOS-401"}`.

---

## MCP Resource URIs

### KB document lookup
```
cosmos://kb/pillar_3_apis_tools/endpoints/cancel_order
→ Returns full KB document for cancel_order API
```

### Health check
```
cosmos://health
→ Returns {"status": "ok", "recall_at_5": 0.83, ...}
```

---

## Rate Limits

MCP tool calls share the same rate limit as HTTP API calls:
- 60 requests/minute per session
- 1,000 requests/hour per company_id

Heavy MCP usage (e.g., bulk KB queries) should use the `/v1/training-pipeline` API directly instead of MCP tools.

---

## Enabling MCP in Development

```bash
# 1. Start COSMOS
npm start

# 2. Test MCP endpoint
curl http://localhost:10001/cosmos/mcp

# 3. Add to Claude Code settings (see above)
# 4. Restart Claude Code session
# 5. Type: "Use cosmos_search to find information about NDR resolution"
```
