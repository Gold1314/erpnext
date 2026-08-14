# Reference MCP Server for the ERPNext Agent Surface

A complete, copy-pasteable stdio MCP server that proxies `tools/list` and
`tools/call` to `erpnext.agent.api.call_tool`.

**This file is documentation, not app code.** An MCP server is a process that
speaks stdio JSON-RPC to a model runner and HTTPS to a Frappe site. It has no
doctypes, it must run when the bench is not running, and it belongs to
whoever operates the model. Shipping it inside `erpnext/` would put an
outward-facing daemon in the app's import path. Save the script below outside
the app — `~/erpnext-mcp/server.py` is fine.

## What it needs from the site

Exactly three whitelisted endpoints, all in `erpnext/agent/api.py`:

| Endpoint | MCP method |
|---|---|
| `erpnext.agent.api.get_tool_catalog` | `tools/list` |
| `erpnext.agent.api.call_tool` | `tools/call` |
| `erpnext.agent.api.get_policy_summary` | (prompt material — see below) |

Nothing else. The guard lives on the server side; a compromised or replaced
MCP client cannot widen it.

## 1. Create a dedicated agent user and API key

Do **not** reuse a human's API key. The `user` column of the Agent Action Log
should name the agent, and revocation should be one disable.

```
Desk → User → New
    Email:      sales-drafting-agent@example.com
    User Type:  System User
    Roles:      Sales User          (only what the agent must do by hand)
    → Settings → API Access → Generate Keys   (note the api_secret; shown once)

Desk → Agent Policy → New            (or list view → "Install Shipped Default")
    Policy Name: Default Agent Policy
    Enabled ✓    Is Default ✓
```

The agent's *effective* permissions are the intersection of its Frappe roles
and the Agent Policy. Both must allow an action; neither can widen the other.

## 2. The server

Python 3.10+, standard library only (`urllib`) so it can run anywhere,
including next to an air-gapped model.

```python
#!/usr/bin/env python3
"""stdio MCP server proxying to the ERPNext guarded agent surface.

Environment:
    ERPNEXT_URL         https://erp.example.com
    ERPNEXT_API_KEY     from User → API Access
    ERPNEXT_API_SECRET  from User → API Access
    ERPNEXT_TIMEOUT     seconds (default 60)

Run:  ERPNEXT_URL=... ERPNEXT_API_KEY=... ERPNEXT_API_SECRET=... python server.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SERVER_NAME = "erpnext-agent"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

BASE_URL = os.environ.get("ERPNEXT_URL", "").rstrip("/")
API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
TIMEOUT = int(os.environ.get("ERPNEXT_TIMEOUT", "60"))

# risk class -> MCP tool annotations, so a client can render a confirmation
# prompt for writes without knowing anything about ERPNext
RISK_ANNOTATIONS = {
    "READ": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    "DRAFT_WRITE": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    "SUBMIT": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    "DESTRUCTIVE": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
}


class FrappeError(Exception):
    pass


def frappe_call(method: str, payload: dict) -> dict:
    """POST to a whitelisted method. Frappe returns {"message": <return value>}."""
    if not (BASE_URL and API_KEY and API_SECRET):
        raise FrappeError("Set ERPNEXT_URL, ERPNEXT_API_KEY and ERPNEXT_API_SECRET.")

    request = urllib.request.Request(
        f"{BASE_URL}/api/method/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"token {API_KEY}:{API_SECRET}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise FrappeError(f"HTTP {exc.code} from {method}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FrappeError(f"Cannot reach {BASE_URL}: {exc.reason}") from exc

    return body.get("message", body)


# ------------------------------------------------------------------ MCP glue


def build_tool_list() -> list[dict]:
    """ERPNext catalog -> MCP tool descriptors.

    The risk class is put in the description as well as the annotations: a
    model that can read "DRAFT_WRITE" wastes fewer turns than one that
    discovers the boundary by being refused.
    """
    catalog = frappe_call("erpnext.agent.api.get_tool_catalog", {})

    tools = []
    for entry in catalog.get("tools", []):
        schema = dict(entry.get("params_schema") or {})
        schema.setdefault("type", "object")
        # 'clamp' is our own keyword; strip it so strict clients do not choke
        for spec in (schema.get("properties") or {}).values():
            spec.pop("clamp", None)

        tools.append(
            {
                "name": entry["name"],
                "description": f"[{entry['risk']}] {entry['description']}",
                "inputSchema": schema,
                "annotations": RISK_ANNOTATIONS.get(entry["risk"], {}),
            }
        )
    return tools


def call_erpnext_tool(name: str, arguments: dict) -> dict:
    """Proxy one tool call. Policy denials come back as ok=false, not as an
    HTTP error, so they are reported to the model as tool errors with the
    policy reason attached — which is what lets it correct course or stop."""
    result = frappe_call(
        "erpnext.agent.api.call_tool",
        {"tool_name": name, "params_json": json.dumps(arguments or {})},
    )

    ok = bool(result.get("ok"))
    if ok:
        text = json.dumps(result.get("result"), indent=2, default=str)
        if result.get("log_warning"):
            text = f"WARNING: {result['log_warning']}\n\n{text}"
    else:
        decision = result.get("decision") or {}
        text = json.dumps(
            {
                "error": result.get("error"),
                "policy_reason": decision.get("reason"),
                "requires_approval": decision.get("requires_approval"),
            },
            indent=2,
            default=str,
        )

    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def handle(request: dict) -> dict | None:
    """Dispatch one JSON-RPC request. Returns None for notifications."""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return reply(
            request_id,
            {
                # echo the client's version when we can speak it
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return reply(request_id, {})

    if method == "tools/list":
        return reply(request_id, {"tools": build_tool_list()})

    if method == "tools/call":
        return reply(
            request_id,
            call_erpnext_tool(params.get("name", ""), params.get("arguments") or {}),
        )

    return error(request_id, -32601, f"Method not found: {method}")


def reply(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except ValueError:
            write(error(None, -32700, "Parse error"))
            continue

        try:
            response = handle(request)
        except FrappeError as exc:
            response = error(request.get("id"), -32000, str(exc))
        except Exception as exc:  # never let one bad call kill the server
            response = error(request.get("id"), -32603, f"{type(exc).__name__}: {exc}")

        if response is not None:
            write(response)


def write(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
```

## 3. Smoke-test it without a model

```bash
export ERPNEXT_URL=https://erp.example.com
export ERPNEXT_API_KEY=xxxxxxxxxxxx
export ERPNEXT_API_SECRET=yyyyyyyyyyyy

printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_documents","arguments":{"doctype":"Sales Order","limit":3}}}' \
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"submit_document","arguments":{"doctype":"Sales Order","name":"SAL-ORD-2026-00001"}}}' \
  | python server.py
```

Request 4 must come back `"isError": true` with a `policy_reason` naming the
`deny SUBMIT on *` rule. **If it does not, stop and fix the policy before
connecting a model.** That single request is the acceptance test for the
whole guard.

Then check the evidence: Desk → **Agent Activity** report, or Agent Action
Log filtered to `decision = Denied`. Four calls in, four rows out.

## 4. Point a model at it

Any MCP-capable client works; the server is a plain stdio subprocess. The
usual config shape:

```json
{
  "mcpServers": {
    "erpnext": {
      "command": "python",
      "args": ["/home/ops/erpnext-mcp/server.py"],
      "env": {
        "ERPNEXT_URL": "https://erp.example.com",
        "ERPNEXT_API_KEY": "xxxxxxxxxxxx",
        "ERPNEXT_API_SECRET": "yyyyyyyyyyyy"
      }
    }
  }
}
```

### Against a self-hosted model

Data sovereignty is the differentiator (see `erpnext/ai/DESIGN.md`), and
nothing in this design requires a hosted model. Run the model locally and
give the loop MCP support:

1. **Serve the model.** Any OpenAI-compatible server — vLLM
   (`vllm serve <model> --port 8000`), llama.cpp's `llama-server`, Ollama's
   `/v1` endpoint, LM Studio. Pick a model with solid tool-calling; the tool
   descriptions and JSON schemas this server advertises are what it has to
   work from.
2. **Run an MCP-capable agent loop** against that endpoint. Several
   open-source loops accept both an OpenAI-compatible base URL and an MCP
   server config; the bridge pattern is always the same:
   - call `tools/list` once at startup, hand the descriptors to the model as
     its tool definitions;
   - on a tool call, forward `{name, arguments}` to `tools/call`;
   - append the returned `content[0].text` as the tool result, error or not.
3. **Nothing changes on the ERP side.** The site sees the same
   `call_tool` requests, applies the same policy, writes the same log rows.
   The guard does not care which model produced the call — which is the whole
   argument for policy over prompt.

### Seed the system prompt from the policy

Give the model the real boundary instead of a hand-written approximation:

```python
summary = frappe_call("erpnext.agent.api.get_policy_summary", {})
```

Returns the user, the policy in force, and per risk class the allowed
doctypes, denied doctypes, amount limits and whether approval is required.
Rendering that into the system prompt saves turns — but it is a
*convenience*. A model that ignores it is refused by the server exactly the
same way.

## Operational notes

- **HTTPS only.** The API secret is a bearer credential in every request.
- **The secret is shown once.** Store it in the client's env config or a
  secret manager, never in the repo. Note that `audit.redact_params` scrubs
  secret-looking keys from logged parameters, so a credential accidentally
  passed as a tool argument does not land in the Agent Action Log — but do
  not rely on that as a control.
- **Row caps are server-side.** `limit` is clamped to 50 and reports to 500
  rows; a client asking for more gets the cap, not an error. Paginate with
  `filters` (e.g. `{"creation": [">", "2026-01-01"]}`), not with a bigger
  `limit`.
- **No rate limiting yet** (see DESIGN.md follow-ups). Until it exists, run
  agents against a site with normal Frappe rate limits configured, and watch
  the Agent Activity report for call volume.
- **One MCP server per agent identity.** Two agents that should have
  different boundaries need two Users, two API keys and role-scoped Agent
  Policy rules — not one shared key and a prompt asking them to behave.
