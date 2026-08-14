# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Agent Action Log writer — *every* agent tool call, allowed or denied.

Blueprint W6: "every agent action logged". The log is the third leg of the
guard, next to policy and Frappe permissions: policy decides, permissions
enforce, the log makes both auditable after the fact. Denials are logged as
loudly as successes — a stream of denied SUBMIT attempts is the signal that
something upstream is trying to walk past the boundary.

The audit row is **append-only** by permission (see
``agent_action_log.json``): the acting user may create rows and read their
own; System Manager / Accounts Manager / Auditor read everything; no role has
``write`` or ``amend``. Nothing here uses ``ignore_permissions``.

The "audit must not break the call path" tradeoff
-------------------------------------------------

:func:`log_action` never raises. A DB hiccup while writing the log must not
turn a successful ``get_document`` into a 500. But silence is not acceptable
either, so:

* every failure is reported to ``frappe.log_error``;
* :func:`log_action` returns ``None`` instead of a log name, and the guarded
  wrapper in ``tools.py`` turns that ``None`` into a ``log_warning`` on the
  tool result **for write-risk tools** (DRAFT_WRITE and above).

That is the honest position: a read whose log was lost is a gap in the
record; a *write* whose log was lost is an unrecorded change to the
database, and the caller is told so in the same response. We do not go
further and roll the write back — the document already exists and silently
deleting it would be a second unlogged mutation. Operators who need
strict "no write without an audit row" semantics should run the log table on
the same transaction-committing store as the data (the default) and treat
any ``log_warning`` as an incident.
"""

from __future__ import annotations

import json

import frappe

LOG_DOCTYPE = "Agent Action Log"

#: substrings that mark a key as likely-secret. Matched against the key with
#: case and separators stripped, so ``api_key``, ``API-KEY``, ``apiKey``,
#: ``x_auth_token`` and ``customer_secret`` are all caught by the same hint.
#: Deliberately blunt: over-redacting a field called ``tokenizer_notes`` costs
#: nothing, under-redacting an API key costs everything.
SECRET_KEY_HINTS = (
	"password",
	"passwd",
	"pwd",
	"apikey",
	"token",
	"secret",
	"credential",
	"privatekey",
	"authorization",
	"authheader",
)

REDACTED = "***redacted***"

#: hard ceiling on the stored parameter blob, so a bulk payload cannot turn
#: the audit table into the largest table on the site
MAX_PARAMS_CHARS = 4000


def is_secret_key(key) -> bool:
	if not isinstance(key, str):
		return False
	normalized = "".join(char for char in key.lower() if char.isalnum())
	return any(hint in normalized for hint in SECRET_KEY_HINTS)


def redact_params(value):
	"""Recursively replace likely-secret values, keeping every other key.

	Pure — no frappe, no I/O — so it is unit-tested directly. Structure is
	preserved (keys stay, nesting stays) because the whole point of the log
	is that a human can read what the agent asked for; only the values behind
	secret-looking keys are replaced.
	"""
	if isinstance(value, dict):
		return {key: (REDACTED if is_secret_key(key) else redact_params(val)) for key, val in value.items()}
	if isinstance(value, list | tuple):
		return [redact_params(item) for item in value]
	return value


def serialize_params(params) -> str:
	"""Redacted, truncated, JSON-encoded parameters for the log row.

	Accepts a dict, a list, or an already-encoded JSON string (in which case
	it is decoded first so redaction can reach inside it).
	"""
	if isinstance(params, str):
		try:
			params = json.loads(params)
		except (TypeError, ValueError):
			return params[:MAX_PARAMS_CHARS]

	try:
		text = json.dumps(redact_params(params), default=str, sort_keys=True)
	except (TypeError, ValueError):
		text = str(redact_params(params))

	if len(text) > MAX_PARAMS_CHARS:
		text = text[:MAX_PARAMS_CHARS] + f"… (truncated at {MAX_PARAMS_CHARS} characters)"
	return text


def log_action(
	tool: str,
	params,
	decision,
	outcome: str,
	doc_reference: tuple[str, str] | None = None,
	error: str | None = None,
	duration_ms: int | None = None,
	risk_level: str | None = None,
	doctype_touched: str | None = None,
) -> str | None:
	"""Write one Agent Action Log row. Never raises; returns the row name or
	``None`` when the write failed (see the module docstring).

	:param tool: catalog tool name.
	:param params: the call parameters — redacted here, never by the caller.
	:param decision: a ``policy.PolicyDecision`` (or anything with
	        ``allowed`` / ``reason``).
	:param outcome: ``Success`` | ``Error`` | ``Denied``.
	:param doc_reference: ``(doctype, name)`` of the document the call
	        produced or touched, when there is one.
	"""
	try:
		reference_doctype, reference_name = doc_reference or (None, None)
		allowed = bool(getattr(decision, "allowed", False))

		log = frappe.get_doc(
			{
				"doctype": LOG_DOCTYPE,
				"user": frappe.session.user,
				"tool": tool,
				"risk_level": risk_level,
				"doctype_touched": doctype_touched,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"decision": "Allowed" if allowed else "Denied",
				"reason": getattr(decision, "reason", None),
				"outcome": outcome,
				"error": (error or "")[:1000] or None,
				"params_json": serialize_params(params),
				"duration_ms": duration_ms,
			}
		)
		log.insert()

		return log.name
	except Exception:
		frappe.log_error(
			title=f"Agent Action Log write failed for tool {tool}",
			message=frappe.get_traceback(),
		)
		return None
