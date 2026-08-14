# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The agent tool catalog and the single guarded execution path.

Every tool an agent can invoke is a row in :data:`TOOL_CATALOG` and every
invocation goes through :func:`_guarded`, which does the same four things in
the same order, always:

1. **Validate parameters** against the tool's JSON schema
   (``policy.validate_params`` — pure). Nothing else runs until the input is
   the shape the handler expects, and ``additionalProperties: False`` means
   no extra key can be smuggled past the boundary.
2. **Evaluate policy** (``policy.evaluate_policy`` — pure) for the *session
   user's* roles, the tool's risk class, the doctype the call touches and,
   where a value exists, the transaction amount.
3. **Execute** the handler, which enforces Frappe's own permissions —
   ``frappe.has_permission(..., throw=True)`` / ``doc.check_permission()`` /
   the permission checks inside ``frappe.get_list`` and ``doc.insert``.
   ``ignore_permissions`` appears nowhere in this module. Policy can only
   *narrow* what the user could already do by hand; it can never widen it.
4. **Log** the call to the Agent Action Log — allowed, denied, succeeded or
   failed, with a duration and a redacted parameter blob.

Three tools that are conspicuously absent
-----------------------------------------

There is no ``delete_document``, no ``cancel_document`` and no
``amend_document``. ``ToolRisk.DESTRUCTIVE`` exists in the risk enum so the
policy engine is complete and so an operator's policy can express "never",
but no handler in this catalog carries that risk. Deleting or cancelling a
posted document is a finance decision with ledger consequences; it belongs
to a human in the desk UI, not to a tool endpoint.

Submit and Workflow precedence
------------------------------

``submit_document`` exists but is **denied by the shipped policy on every
doctype**. Even after an operator adds a rule that allows it, two further
gates apply, in this order:

    active Workflow  >  policy rule  >  Frappe submit permission

If the doctype has an active **Workflow**, the tool refuses outright and
tells the agent to use the workflow action instead — regardless of what the
policy rule says. A workflow *is* the organisation's approval design; a
policy rule that let an agent jump it would silently delete an approval
step. Only when there is no active workflow does the policy rule decide, and
even then ``doc.submit()`` still runs Frappe's own submit permission check.
"""

from __future__ import annotations

import time

import frappe
from frappe import _
from frappe.utils import cint, flt, get_link_to_form

from erpnext.agent import audit
from erpnext.agent.policy import (
	DEFAULT_DENY_RISKS,
	DEFAULT_POLICY_RULES,
	TOOL_CATALOG_DOCTYPE,
	PolicyDecision,
	ToolRisk,
	evaluate_policy,
	validate_params,
)

POLICY_DOCTYPE = "Agent Policy"

#: hard row ceilings. The schema clamps ``limit`` to this for list reads; the
#: report cap is applied after execution because a report computes its own
#: row set and cannot be told to stop early.
MAX_LIST_ROWS = 50
MAX_REPORT_ROWS = 500

#: report types an agent may run. "Report Builder" is excluded — it is a
#: saved list view, which ``search_documents`` already covers with better
#: guarantees. "Custom Report" inherits its parent's type and is resolved.
RUNNABLE_REPORT_TYPES = ("Script Report", "Query Report")

#: fieldtypes never returned by any read tool
SECRET_FIELDTYPES = frozenset({"Password"})

#: layout-only fieldtypes omitted from ``get_doctype_schema`` — they carry no
#: information an agent can act on. Every data-bearing fieldtype is kept.
LAYOUT_FIELDTYPES = frozenset(
	{"Section Break", "Column Break", "Tab Break", "HTML", "Heading", "Image", "Button", "Fold"}
)

#: standard columns readable on any doctype
STANDARD_FIELDS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"parent",
		"parenttype",
		"parentfield",
	}
)

#: payload keys that would reach into the document *machinery* rather than
#: its data — refused by the write tools
FORBIDDEN_PAYLOAD_KEYS = frozenset(
	{
		"flags",
		"ignore_permissions",
		"ignore_mandatory",
		"ignore_validate",
		"ignore_links",
		"ignore_version",
		"_ignore_links",
		"__islocal",
		"__unsaved",
	}
)

#: fields consulted, in order, when working out the amount a policy
#: ``max_amount`` cap should be tested against
AMOUNT_FIELDS = ("base_grand_total", "grand_total", "total", "amount", "paid_amount")


TOOL_CATALOG: dict[str, dict] = {}


def tool(name: str, risk: str, description: str, params_schema: dict, doctype_param: str | None = "doctype"):
	"""Register a handler in :data:`TOOL_CATALOG`."""

	def decorator(handler):
		TOOL_CATALOG[name] = {
			"name": name,
			"description": description,
			"risk": risk,
			"params_schema": params_schema,
			"handler": handler,
			"doctype_param": doctype_param,
		}
		return handler

	return decorator


# ------------------------------------------------------------------ helpers


def get_active_policy_rules() -> tuple[list[dict], str]:
	"""The rules in force, plus a label naming where they came from.

	The enabled **Agent Policy** flagged ``is_default`` wins. When no such
	document exists — a fresh site, or an operator who disabled every policy
	— the shipped :data:`DEFAULT_POLICY_RULES` apply. Falling back to the
	safe default rather than to "no rules" matters: an empty rule list is
	already default-deny, but the shipped default keeps read-only agents
	working while still refusing every submit.
	"""
	name = frappe.db.get_value(POLICY_DOCTYPE, {"enabled": 1, "is_default": 1}, "name")
	if name:
		policy = frappe.get_cached_doc(POLICY_DOCTYPE, name)
		rules = [row.as_dict() for row in (policy.rules or [])]
		if rules:
			return rules, name

	return list(DEFAULT_POLICY_RULES), _("shipped default policy")


def _meta(doctype: str):
	if not doctype or not isinstance(doctype, str):
		frappe.throw(_("A doctype is required."))
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Unknown doctype {0}.").format(doctype))
	return frappe.get_meta(doctype)


def _safe_fields(meta, fields: list | None) -> list[str]:
	"""Only real fieldnames of this doctype. Anything else is refused rather
	than passed through — a ``fields`` list is an expression slot in the
	query builder and must never carry agent-authored text."""
	if not fields:
		return ["name"]

	safe = ["name"]
	for raw in fields:
		fieldname = (raw or "").strip() if isinstance(raw, str) else ""
		if not fieldname or fieldname in safe:
			continue
		if fieldname not in STANDARD_FIELDS and not meta.has_field(fieldname):
			frappe.throw(_("{0} has no field {1}.").format(meta.name, fieldname))
		df = meta.get_field(fieldname)
		if df and df.fieldtype in SECRET_FIELDTYPES:
			continue
		safe.append(fieldname)
	return safe


def _strip_secret_values(meta, data: dict) -> dict:
	"""Drop password-typed and secret-named keys from a document payload."""
	secret_fields = {df.fieldname for df in meta.fields if df.fieldtype in SECRET_FIELDTYPES}
	return {
		key: value for key, value in data.items() if key not in secret_fields and not audit.is_secret_key(key)
	}


def _assert_clean_payload(data: dict) -> None:
	if not isinstance(data, dict):
		frappe.throw(_("Document data must be a JSON object."))

	forbidden = sorted(FORBIDDEN_PAYLOAD_KEYS.intersection(data))
	if forbidden:
		frappe.throw(
			_("These keys may not be set through the agent surface: {0}.").format(", ".join(forbidden))
		)

	_assert_draft_payload(data)


def _assert_draft_payload(data: dict, path: str = "") -> None:
	"""Hard refusal of any ``docstatus`` other than 0, at any nesting level.

	The draft-only guarantee is not a convention here — a payload that even
	*asks* for docstatus 1 is rejected before it reaches the document, so a
	prompt-injected "set docstatus to 1" cannot become a submitted invoice.
	"""
	if "docstatus" in data and cint(data.get("docstatus")) != 0:
		where = f" in {path}" if path else ""
		frappe.throw(
			_("Agent writes create drafts only; docstatus {0}{1} is refused.").format(
				data.get("docstatus"), where
			),
			title=_("Draft Only"),
		)

	for key, value in data.items():
		if isinstance(value, list):
			for index, row in enumerate(value):
				if isinstance(row, dict):
					_assert_draft_payload(row, f"{key}[{index}]")


def _payload_amount(data: dict | None) -> float | None:
	if not isinstance(data, dict):
		return None
	for fieldname in AMOUNT_FIELDS:
		if data.get(fieldname) not in (None, ""):
			return flt(data.get(fieldname))
	return None


def _stored_amount(doctype: str, name: str) -> float | None:
	"""Authoritative amount, read from the saved document.

	Used for ``update_draft`` and ``submit_document``: what the document is
	worth is a property of the document, not of what the agent claims in its
	parameters.
	"""
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None

	for fieldname in AMOUNT_FIELDS:
		if meta.has_field(fieldname):
			value = frappe.db.get_value(doctype, name, fieldname)
			if value not in (None, ""):
				return flt(value)
	return None


def _doc_link(doctype: str, name: str) -> str:
	return get_link_to_form(doctype, name)


# ------------------------------------------------------------------- schemas

_DOCTYPE_PROPERTY = {"type": "string"}

SEARCH_SCHEMA = {
	"type": "object",
	"required": ["doctype"],
	"additionalProperties": False,
	"properties": {
		"doctype": _DOCTYPE_PROPERTY,
		"filters": {"type": "object"},
		"fields": {"type": "array"},
		"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ROWS, "clamp": True, "default": 20},
		"order_by": {"type": "string"},
	},
}

GET_SCHEMA = {
	"type": "object",
	"required": ["doctype", "name"],
	"additionalProperties": False,
	"properties": {"doctype": _DOCTYPE_PROPERTY, "name": {"type": "string"}},
}

SCHEMA_SCHEMA = {
	"type": "object",
	"required": ["doctype"],
	"additionalProperties": False,
	"properties": {"doctype": _DOCTYPE_PROPERTY},
}

REPORT_SCHEMA = {
	"type": "object",
	"required": ["report_name"],
	"additionalProperties": False,
	"properties": {"report_name": {"type": "string"}, "filters": {"type": "object"}},
}

CREATE_SCHEMA = {
	"type": "object",
	"required": ["doctype", "data"],
	"additionalProperties": False,
	"properties": {"doctype": _DOCTYPE_PROPERTY, "data": {"type": "object"}},
}

UPDATE_SCHEMA = {
	"type": "object",
	"required": ["doctype", "name", "data"],
	"additionalProperties": False,
	"properties": {
		"doctype": _DOCTYPE_PROPERTY,
		"name": {"type": "string"},
		"data": {"type": "object"},
	},
}

SUBMIT_SCHEMA = {
	"type": "object",
	"required": ["doctype", "name"],
	"additionalProperties": False,
	"properties": {"doctype": _DOCTYPE_PROPERTY, "name": {"type": "string"}},
}

EMPTY_SCHEMA = {"type": "object", "required": [], "additionalProperties": False, "properties": {}}


# ------------------------------------------------------------------- handlers


@tool(
	"search_documents",
	ToolRisk.READ,
	"List documents of a doctype matching filters. Row-level permissions and "
	"User Permissions are applied by the server; results are capped at "
	f"{MAX_LIST_ROWS} rows.",
	SEARCH_SCHEMA,
)
def search_documents(
	doctype: str,
	filters: dict | None = None,
	fields: list | None = None,
	limit: int = 20,
	order_by: str | None = None,
):
	meta = _meta(doctype)
	frappe.has_permission(doctype, "read", throw=True)

	safe_fields = _safe_fields(meta, fields)
	order = None
	if order_by:
		# only a plain fieldname plus an optional direction; never a free expression
		parts = order_by.strip().split()
		fieldname = parts[0]
		if fieldname not in STANDARD_FIELDS and not meta.has_field(fieldname):
			frappe.throw(_("{0} has no field {1}.").format(doctype, fieldname))
		direction = parts[1].lower() if len(parts) > 1 else "desc"
		if direction not in ("asc", "desc"):
			frappe.throw(_("Order direction must be asc or desc."))
		order = f"`tab{doctype}`.`{fieldname}` {direction}"

	rows = frappe.get_list(
		doctype,
		filters=filters or {},
		fields=safe_fields,
		limit_page_length=min(cint(limit) or 20, MAX_LIST_ROWS),
		order_by=order,
	)
	return {
		"doctype": doctype,
		"count": len(rows),
		"rows": rows,
		"limit": min(cint(limit) or 20, MAX_LIST_ROWS),
	}


@tool(
	"get_document",
	ToolRisk.READ,
	"Fetch one document in full, including its child tables. Password fields are stripped from the response.",
	GET_SCHEMA,
)
def get_document(doctype: str, name: str):
	meta = _meta(doctype)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")

	data = doc.as_dict()
	clean = _strip_secret_values(meta, data)

	for df in meta.get_table_fields():
		rows = clean.get(df.fieldname) or []
		child_meta = frappe.get_meta(df.options)
		clean[df.fieldname] = [_strip_secret_values(child_meta, dict(row)) for row in rows]

	return {
		"doctype": doctype,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"document": clean,
		"link": _doc_link(doctype, doc.name),
	}


@tool(
	"get_doctype_schema",
	ToolRisk.READ,
	"Describe a doctype: its fields, types, mandatory flags, link targets and "
	"child tables. This is how an agent learns the data model instead of "
	"guessing it.",
	SCHEMA_SCHEMA,
)
def get_doctype_schema(doctype: str):
	meta = _meta(doctype)
	frappe.has_permission(doctype, "read", throw=True)

	fields = []
	for df in meta.fields:
		if df.fieldtype in SECRET_FIELDTYPES or df.fieldtype in LAYOUT_FIELDTYPES:
			continue
		if audit.is_secret_key(df.fieldname):
			continue
		fields.append(
			{
				"fieldname": df.fieldname,
				"label": df.label,
				"fieldtype": df.fieldtype,
				"reqd": bool(df.reqd),
				"read_only": bool(df.read_only),
				"hidden": bool(df.hidden),
				"options": df.options,
				"default": df.default,
				"description": df.description,
				"in_list_view": bool(df.in_list_view),
			}
		)

	return {
		"doctype": meta.name,
		"module": meta.module,
		"is_submittable": bool(meta.is_submittable),
		"is_tree": bool(getattr(meta, "is_tree", 0)),
		"istable": bool(meta.istable),
		"title_field": meta.title_field,
		"autoname": meta.autoname,
		"naming_rule": getattr(meta, "naming_rule", None),
		"fields": fields,
		"child_tables": [
			{"fieldname": df.fieldname, "label": df.label, "child_doctype": df.options}
			for df in meta.get_table_fields()
		],
	}


@tool(
	"run_report",
	ToolRisk.READ,
	"Run a Script or Query Report the user is allowed to see. Returns at most "
	f"{MAX_REPORT_ROWS} rows; a truncated result says so explicitly.",
	REPORT_SCHEMA,
	doctype_param=None,
)
def run_report(report_name: str, filters: dict | None = None):
	from frappe.desk.query_report import run as run_query_report

	report = frappe.get_doc("Report", report_name)
	report_type = report.report_type
	if report_type == "Custom Report" and report.reference_report:
		report_type = frappe.db.get_value("Report", report.reference_report, "report_type")

	if report_type not in RUNNABLE_REPORT_TYPES:
		frappe.throw(
			_("Only {0} can be run through the agent surface; {1} is a {2}.").format(
				" and ".join(RUNNABLE_REPORT_TYPES), report_name, report.report_type
			)
		)
	if report.disabled:
		frappe.throw(_("Report {0} is disabled.").format(report_name))

	# both gates: the Report record's own role list, and read access to the
	# doctype it reports on
	if not report.is_permitted():
		raise frappe.PermissionError(_("You are not permitted to run report {0}.").format(report_name))
	if report.ref_doctype:
		frappe.has_permission(report.ref_doctype, "report", throw=True)

	result = run_query_report(report_name=report_name, filters=filters or {}, ignore_prepared_report=True)

	rows = result.get("result") or []
	truncated = len(rows) > MAX_REPORT_ROWS
	return {
		"report": report_name,
		"ref_doctype": report.ref_doctype,
		"columns": result.get("columns") or [],
		"rows": rows[:MAX_REPORT_ROWS],
		"row_count": min(len(rows), MAX_REPORT_ROWS),
		"truncated": truncated,
		"message": result.get("message"),
	}


@tool(
	"create_draft",
	ToolRisk.DRAFT_WRITE,
	"Insert a new document as a draft (docstatus 0). A payload asking for any "
	"other docstatus is refused. Nothing an agent creates posts to the ledger "
	"or moves stock until a human submits it.",
	CREATE_SCHEMA,
)
def create_draft(doctype: str, data: dict):
	_meta(doctype)
	_assert_clean_payload(data)
	frappe.has_permission(doctype, "create", throw=True)

	payload = {key: value for key, value in data.items() if key != "doctype"}
	doc = frappe.get_doc({"doctype": doctype, **payload})
	doc.docstatus = 0
	doc.insert()  # permissions enforced by frappe; never ignore_permissions

	return {
		"doctype": doctype,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"link": _doc_link(doctype, doc.name),
		"message": _("Draft {0} created. A human must review and submit it.").format(doc.name),
	}


@tool(
	"update_draft",
	ToolRisk.DRAFT_WRITE,
	"Update a document that is still a draft (docstatus 0). Submitted and "
	"cancelled documents are never touched.",
	UPDATE_SCHEMA,
)
def update_draft(doctype: str, name: str, data: dict):
	_meta(doctype)
	_assert_clean_payload(data)

	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")

	if doc.docstatus != 0:
		frappe.throw(
			_("{0} {1} is {2}, not a draft; the agent surface only edits drafts.").format(
				doctype, name, "cancelled" if doc.docstatus == 2 else "submitted"
			),
			title=_("Draft Only"),
		)

	payload = {key: value for key, value in data.items() if key not in ("doctype", "name")}
	doc.update(payload)
	doc.docstatus = 0
	doc.save()

	return {
		"doctype": doctype,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"updated_fields": sorted(payload),
		"link": _doc_link(doctype, doc.name),
	}


@tool(
	"submit_document",
	ToolRisk.SUBMIT,
	"Submit a draft. DENIED BY THE SHIPPED POLICY on every doctype. Even when "
	"an operator's policy allows it, a doctype with an active Workflow is "
	"refused — the workflow action is the approval path.",
	SUBMIT_SCHEMA,
)
def submit_document(doctype: str, name: str):
	_meta(doctype)

	workflow = frappe.db.exists("Workflow", {"document_type": doctype, "is_active": 1})
	if workflow:
		frappe.throw(
			_(
				"{0} is governed by the active Workflow '{1}'. Submitting it directly would skip an "
				"approval step. Apply the workflow action instead (the document's Actions menu, or "
				"frappe.model.workflow.apply_workflow)."
			).format(doctype, workflow),
			title=_("Workflow Approval Required"),
		)

	doc = frappe.get_doc(doctype, name)
	doc.check_permission("submit")

	if doc.docstatus != 0:
		frappe.throw(_("{0} {1} is not a draft.").format(doctype, name))

	doc.submit()

	return {
		"doctype": doctype,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"link": _doc_link(doctype, doc.name),
	}


@tool(
	"list_tools",
	ToolRisk.READ,
	"Describe every tool on this surface with its risk class and parameter "
	"schema, so an MCP server can advertise it.",
	EMPTY_SCHEMA,
	doctype_param=None,
)
def list_tools():
	return {"tools": get_catalog()}


# ------------------------------------------------------------ catalog access


def get_catalog() -> list[dict]:
	"""The catalog as JSON — handlers omitted, order stable."""
	return [
		{
			"name": entry["name"],
			"description": entry["description"],
			"risk": entry["risk"],
			"params_schema": entry["params_schema"],
		}
		for entry in TOOL_CATALOG.values()
	]


def _resolve_doctype(entry: dict, params: dict) -> str:
	"""The doctype a call touches, for the policy decision.

	``run_report`` resolves to the report's ``ref_doctype``, so a report is
	governed by the read allowlist of the data it reports on rather than by a
	separate list of report names. Tools that touch no document at all resolve
	to the :data:`TOOL_CATALOG_DOCTYPE` pseudo-doctype so they are still
	policy-evaluated instead of bypassing the guard.
	"""
	if entry["name"] == "run_report":
		ref = frappe.db.get_value("Report", params.get("report_name"), "ref_doctype")
		return ref or "Report"

	key = entry.get("doctype_param")
	if not key:
		return TOOL_CATALOG_DOCTYPE
	return (params.get(key) or "").strip()


def _resolve_amount(entry: dict, params: dict):
	"""Value to test a ``max_amount`` cap against, or ``None`` when unknown.

	For creates this is best-effort — it can only read what the payload
	carries, since the document does not exist yet. For updates and submits it
	is read from the **stored** document, which is authoritative. A rule with
	a cap and an unknown amount denies (``policy`` fails closed), which is why
	the shipped DRAFT_WRITE rules set no cap.
	"""
	name = entry["name"]
	if name == "create_draft":
		return _payload_amount(params.get("data"))
	if name == "update_draft":
		doctype, docname = params.get("doctype"), params.get("name")
		# Test the cap against the larger of the stored and proposed amounts.
		# Taking the payload first would let an agent declare a small
		# ``grand_total`` to slip under a cap, since ``save`` recalculates the
		# real total from the lines afterwards; taking the stored value alone
		# would miss an update that raises the amount above the cap.
		candidates = [
			amount
			for amount in (_stored_amount(doctype, docname), _payload_amount(params.get("data")))
			if amount is not None
		]
		return max(candidates) if candidates else None
	if name == "submit_document":
		return _stored_amount(params.get("doctype"), params.get("name"))
	return None


# -------------------------------------------------------------- the guard


def _guarded(tool_name: str, params: dict | None = None) -> dict:
	"""Validate → decide → execute → log. The only way a tool ever runs."""
	started = time.monotonic()
	params = params or {}
	entry = TOOL_CATALOG.get(tool_name)
	risk = entry["risk"] if entry else None

	if not entry:
		decision = evaluate_policy(tool_name, None, None, [], [])
		audit.log_action(tool_name, params, decision, "Denied", risk_level=None)
		return _failure(tool_name, decision, decision.reason, risk)

	cleaned, errors = validate_params(entry["params_schema"], params)
	if errors:
		reason = _("Parameter validation failed: {0}").format(" ".join(errors))
		decision = PolicyDecision(False, reason, risk in DEFAULT_DENY_RISKS)
		audit.log_action(tool_name, params, decision, "Denied", risk_level=risk)
		return _failure(tool_name, decision, reason, risk)

	rules, policy_source = get_active_policy_rules()
	roles = frappe.get_roles(frappe.session.user)

	try:
		doctype = _resolve_doctype(entry, cleaned)
		amount = _resolve_amount(entry, cleaned)
	except Exception as exc:
		decision = PolicyDecision(False, str(exc) or _("Could not resolve the target of this call."), False)
		audit.log_action(tool_name, cleaned, decision, "Denied", risk_level=risk, error=str(exc))
		return _failure(tool_name, decision, decision.reason, risk)

	decision = evaluate_policy(tool_name, risk, doctype, rules, roles, amount=amount)
	if not decision.allowed:
		audit.log_action(
			tool_name,
			cleaned,
			decision,
			"Denied",
			risk_level=risk,
			doctype_touched=doctype,
			duration_ms=_elapsed_ms(started),
		)
		return _failure(tool_name, decision, decision.reason, risk, policy_source=policy_source)

	try:
		result = entry["handler"](**cleaned)
	except Exception as exc:
		message = str(exc) or exc.__class__.__name__
		audit.log_action(
			tool_name,
			cleaned,
			decision,
			"Error",
			risk_level=risk,
			doctype_touched=doctype,
			error=message,
			duration_ms=_elapsed_ms(started),
		)
		frappe.log_error(title=f"Agent tool {tool_name} failed", message=frappe.get_traceback())
		return _failure(tool_name, decision, message, risk, policy_source=policy_source)

	duration_ms = _elapsed_ms(started)
	reference = None
	if isinstance(result, dict) and result.get("doctype") and result.get("name"):
		reference = (result["doctype"], result["name"])

	log_name = audit.log_action(
		tool_name,
		cleaned,
		decision,
		"Success",
		doc_reference=reference,
		risk_level=risk,
		doctype_touched=doctype,
		duration_ms=duration_ms,
	)

	response = {
		"ok": True,
		"tool": tool_name,
		"risk": risk,
		"result": result,
		"decision": decision.as_dict(),
		"policy_source": policy_source,
		"duration_ms": duration_ms,
		"audit_log": log_name,
	}

	# a write whose audit row was lost is an unrecorded change — say so in the
	# same response rather than letting it pass silently (see audit.py)
	if log_name is None and risk != ToolRisk.READ:
		response["log_warning"] = _(
			"This {0} call succeeded but its Agent Action Log row could not be written. "
			"The change is NOT in the audit trail — investigate before relying on it."
		).format(risk)

	return response


def _elapsed_ms(started: float) -> int:
	return int((time.monotonic() - started) * 1000)


def _failure(
	tool_name: str, decision, message: str, risk: str | None, policy_source: str | None = None
) -> dict:
	return {
		"ok": False,
		"tool": tool_name,
		"risk": risk,
		"error": message,
		"decision": decision.as_dict() if hasattr(decision, "as_dict") else decision,
		"policy_source": policy_source,
	}


def run_tool(tool_name: str, params: dict | None = None) -> dict:
	"""Public entry point for the guarded path (``api.call_tool`` wraps it)."""
	return _guarded(tool_name, params)
