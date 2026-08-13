# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The whitelisted entry points an MCP server or agent framework calls.

Three endpoints, deliberately: one to *do* something, one to discover what
can be done, one to discover what this particular user is allowed to do.

    call_tool(tool_name, params_json)   →  the single guarded action path
    get_tool_catalog()                  →  MCP ``tools/list``
    get_policy_summary()                →  the boundary, as data

Why one entry point and not one endpoint per tool
-------------------------------------------------

A single ``call_tool`` means there is exactly one place where policy is
evaluated, permissions are enforced and the audit row is written — the
guarded path in ``tools._guarded``. Adding a tool cannot accidentally add an
unguarded endpoint, because a tool is a catalog row, not a
``@frappe.whitelist()``.

Why ``get_policy_summary`` exists
---------------------------------

So the system prompt can *describe* the policy instead of *being* it. An
agent that knows it may not submit will not waste turns trying; an agent
that tries anyway is refused by :func:`call_tool` all the same. The prompt is
a convenience; the guard is in ``policy.py``.

Errors are returned, not raised
-------------------------------

``call_tool`` returns ``{"ok": false, "error": …, "decision": {…}}`` for
policy denials, schema violations, permission errors and handler exceptions
alike. An agent loop consumes a result, not a stack trace, and a denial with
a readable ``reason`` lets it correct course or stop. Frappe still records
the traceback via ``frappe.log_error`` inside the guard.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from erpnext.agent import tools
from erpnext.agent.policy import (
	RISK_LEVELS,
	summarize_policy,
	validate_params,
)

#: Re-exported so ``erpnext.agent.api.validate_params`` resolves: the pure
#: parameter validator physically lives in ``policy.py``, next to the other
#: pure functions, so it can be unit-tested without frappe. It is applied to
#: every call inside ``tools._guarded``.
__all__ = ("call_tool", "get_policy_summary", "get_tool_catalog", "validate_params")


def _parse_params(params_json) -> dict:
	"""Accept a JSON string (HTTP/MCP transport) or an already-decoded dict
	(a python caller). Anything else is a parameter error, not a crash."""
	if params_json in (None, "", b""):
		return {}
	if isinstance(params_json, dict):
		return params_json
	if isinstance(params_json, str | bytes):
		try:
			decoded = json.loads(params_json)
		except (TypeError, ValueError) as exc:
			raise ValueError(_("params_json is not valid JSON: {0}").format(exc)) from exc
		if not isinstance(decoded, dict):
			raise ValueError(_("params_json must decode to a JSON object."))
		return decoded

	raise ValueError(_("params_json must be a JSON object or a JSON string."))


@frappe.whitelist()
def call_tool(tool_name: str, params_json=None) -> dict:
	"""Run one catalog tool under policy, permissions and audit.

	:param tool_name: a name from :func:`get_tool_catalog`.
	:param params_json: JSON object (or JSON string) of tool parameters.
	:returns: ``{ok, tool, risk, result | error, decision, policy_source,
	          duration_ms, audit_log}``. ``decision`` always carries a
	          non-empty ``reason`` — pass it back to the model.
	"""
	try:
		params = _parse_params(params_json)
	except ValueError as exc:
		return {
			"ok": False,
			"tool": tool_name,
			"error": str(exc),
			"decision": {"allowed": False, "reason": str(exc), "requires_approval": False},
		}

	return tools.run_tool(tool_name, params)


@frappe.whitelist()
def get_tool_catalog() -> dict:
	"""Every tool, its risk class and its parameter schema.

	Readable by any authenticated user — it describes the surface, not the
	data. What a given user may actually *do* with it comes from
	:func:`get_policy_summary`.
	"""
	return {
		"tools": tools.get_catalog(),
		"risk_levels": list(RISK_LEVELS),
		"limits": {"max_list_rows": tools.MAX_LIST_ROWS, "max_report_rows": tools.MAX_REPORT_ROWS},
	}


@frappe.whitelist()
def get_policy_summary(user: str | None = None) -> dict:
	"""What the current user may do under the policy in force.

	``user`` may only be passed by a System Manager — asking "what can
	*someone else's* agent do?" is an administrative question, and letting any
	user ask it would leak the shape of other people's access.
	"""
	if user and user != frappe.session.user:
		frappe.only_for("System Manager")
	user = user or frappe.session.user

	rules, source = tools.get_active_policy_rules()
	summary = summarize_policy(rules, frappe.get_roles(user))
	summary["user"] = user
	summary["policy_source"] = source
	summary["tools"] = [{"name": entry["name"], "risk": entry["risk"]} for entry in tools.get_catalog()]
	return summary
