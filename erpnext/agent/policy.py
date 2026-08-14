# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The agent policy engine — **pure**, deterministic, no frappe import.

This module is a *security boundary*. Everything in it is a plain function
over plain dicts so it can be exhaustively unit-tested without a site (see
``test_agent.py``); nothing here reads the database, the session, or the
environment. The frappe-facing layers (``tools.py``, ``api.py``) load the
rules and the acting user's roles and hand them in.

Doctrine
--------

**Default deny.** :func:`evaluate_policy` starts from "no", not from "yes".
An action is permitted only when a rule *explicitly* matches it. This is
what makes the guardrail a policy and not a prompt: no wording an agent (or
an attacker steering an agent) can produce changes the outcome, because the
model's text never reaches this function — only a tool name, a risk class, a
doctype and an amount do.

**SUBMIT and DESTRUCTIVE are never autonomous.** Both are in
:data:`DEFAULT_DENY_RISKS`; the shipped :data:`DEFAULT_POLICY_RULES` carry
explicit ``*`` deny rows for them, and every decision about them comes back
with ``requires_approval = True`` even when an operator's own policy allows
them — the flag tells the caller "a human or a workflow owns this step".

**Fail closed on missing information.** No doctype, an unknown risk level,
or an amount that cannot be verified against a cap all resolve to *deny*
with a reason, never to *allow*.

Rule shape
----------

A rule is a plain dict (the **Agent Policy Rule** child row, or a literal
from :data:`DEFAULT_POLICY_RULES`)::

    {
        "risk_level": "DRAFT_WRITE",  # one of RISK_LEVELS
        "doctype_pattern": "Sales Order",  # exact doctype name, or "*"
        "allow": 1,  # 0 = explicit deny
        "role": "Sales User",  # optional; rule applies only to holders
        "max_amount": 50000,  # optional cap; 0/blank = unlimited
        "notes": "…",
    }

Precedence (:func:`evaluate_policy`)
------------------------------------

1. Only rules whose ``risk_level`` equals the requested risk are considered.
2. A rule with a ``role`` is skipped unless the acting user holds that role.
   A rule without a role applies to everyone.
3. A rule whose ``doctype_pattern`` is neither the exact doctype nor ``*``
   is skipped.
4. Of what survives, the **most specific** rule wins:
   ``exact doctype (+2)`` beats ``wildcard (0)``, and within the same
   doctype tier a ``role``-scoped rule (+1) beats an unscoped one. So an
   exact-doctype rule outranks a wildcard rule *even when the wildcard rule
   is role-scoped* — the doctype axis dominates, as specified.
5. Ties are broken **deny-first** (an explicit deny at the same specificity
   beats an allow), then by the rule's position in the list. Both tie-breaks
   are deterministic, so the same inputs always produce the same decision.
6. The winning rule decides on its own; evaluation does **not** fall through
   to a less specific rule when the winner denies, or when the winner allows
   but its ``max_amount`` is exceeded. A cap is a denial with a reason, not
   an invitation to look for a laxer rule.

``max_amount``
--------------

``None`` — and, deliberately, ``0`` — mean *unlimited*. Frappe stores a
blank Currency field as ``0.0``, so treating 0 as "cap of zero" would turn
every rule an operator leaves blank into a silent total block. When a cap
*is* set and the request carries no amount (or an uncoercible one), the
decision is **deny**: an unverifiable amount is not a small amount.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# --------------------------------------------------------------- risk model


class ToolRisk:
	"""Risk classes a tool can carry. Plain string constants — they travel
	through JSON, the Agent Policy Rule Select field and log rows unchanged."""

	READ = "READ"
	DRAFT_WRITE = "DRAFT_WRITE"
	SUBMIT = "SUBMIT"
	DESTRUCTIVE = "DESTRUCTIVE"


#: canonical ordering, low risk first — also the Select options on Agent Policy Rule
RISK_LEVELS = (ToolRisk.READ, ToolRisk.DRAFT_WRITE, ToolRisk.SUBMIT, ToolRisk.DESTRUCTIVE)

#: risk classes that are denied unless an explicit rule says otherwise *and*
#: that always come back flagged ``requires_approval``
DEFAULT_DENY_RISKS = frozenset({ToolRisk.SUBMIT, ToolRisk.DESTRUCTIVE})

#: the "any doctype" pattern in a rule
WILDCARD = "*"

#: pseudo-doctype used by tools that touch no document at all (``list_tools``).
#: Giving them a name means they are still policy-evaluated and still logged,
#: instead of being a hole in the middle of the guard.
TOOL_CATALOG_DOCTYPE = "Agent Tool Catalog"


@dataclass(frozen=True)
class PolicyDecision:
	"""The verdict. ``reason`` is always a non-empty, human-readable string —
	it is shown to the operator in the Agent Action Log and handed back to the
	agent so it can explain itself instead of retrying blindly."""

	allowed: bool
	reason: str
	requires_approval: bool = False
	#: name/pattern of the rule that decided, when one did — for the audit trail
	matched_rule: dict | None = field(default=None, compare=False)

	def as_dict(self) -> dict:
		return {
			"allowed": bool(self.allowed),
			"reason": self.reason,
			"requires_approval": bool(self.requires_approval),
			"matched_rule": self.matched_rule,
		}


# ------------------------------------------------------------ rule handling


def _as_bool(value) -> bool:
	"""Frappe Check fields arrive as 0/1 ints, JSON as true/false, forms as
	"0"/"1" strings. The string "0" is truthy in Python, so it gets its own
	case — getting this wrong would turn an explicit deny into an allow."""
	if isinstance(value, str):
		return value.strip().lower() not in ("", "0", "false", "no")
	return bool(value)


def _as_amount(value) -> float | None:
	"""Coerce to float, or None when the value is absent or uncoercible."""
	if value is None or value == "":
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def normalize_rule(raw: dict) -> dict:
	"""Coerce one raw rule dict into the canonical shape used internally.

	Accepts Agent Policy Rule rows, ``DEFAULT_POLICY_RULES`` literals and
	anything JSON-shaped. ``max_amount`` of 0 (Frappe's stored value for a
	blank Currency) normalises to ``None`` = unlimited.
	"""
	raw = raw or {}
	max_amount = _as_amount(raw.get("max_amount"))
	if max_amount is not None and max_amount <= 0:
		max_amount = None

	return {
		"risk_level": (raw.get("risk_level") or "").strip(),
		"doctype_pattern": (raw.get("doctype_pattern") or "").strip(),
		"allow": _as_bool(raw.get("allow")),
		"role": (raw.get("role") or "").strip() or None,
		"max_amount": max_amount,
		"notes": (raw.get("notes") or "").strip() or None,
	}


def normalize_rules(raw_rules) -> list[dict]:
	return [normalize_rule(rule) for rule in (raw_rules or [])]


def describe_rule(rule: dict) -> str:
	"""One-line human description of a rule, used inside decision reasons."""
	parts = [
		"{action} {risk} on {pattern}".format(
			action="allow" if rule["allow"] else "deny",
			risk=rule["risk_level"] or "?",
			pattern=rule["doctype_pattern"] or "?",
		)
	]
	if rule["role"]:
		parts.append(f"for role {rule['role']}")
	if rule["max_amount"] is not None:
		parts.append(f"up to {rule['max_amount']:g}")
	return " ".join(parts)


def _specificity(rule: dict, doctype: str) -> int:
	"""Higher wins. The doctype axis dominates the role axis by construction:
	an exact doctype scores 2, a role only 1, so exact-without-role (2) still
	outranks wildcard-with-role (1)."""
	score = 2 if rule["doctype_pattern"] == doctype else 0
	if rule["role"]:
		score += 1
	return score


def _matching_rules(
	rules: list[dict], risk: str, doctype: str, roles: set[str]
) -> list[tuple[int, int, int, dict]]:
	"""Sort key tuples for every rule that applies, best first.

	Key is ``(-specificity, allow, index)``: most specific first, then
	deny (``allow`` False sorts as 0) before allow at the same specificity,
	then declaration order.
	"""
	candidates = []
	for index, rule in enumerate(rules):
		if rule["risk_level"] != risk:
			continue
		if rule["role"] and rule["role"] not in roles:
			continue
		if rule["doctype_pattern"] not in (doctype, WILDCARD):
			continue
		candidates.append((-_specificity(rule, doctype), int(rule["allow"]), index, rule))

	candidates.sort(key=lambda item: item[:3])
	return candidates


# ----------------------------------------------------------------- evaluate


def evaluate_policy(
	tool_name: str,
	risk: str | None,
	doctype: str | None,
	policy_rules: list[dict],
	user_roles: list[str],
	amount=None,
) -> PolicyDecision:
	"""Decide whether ``tool_name`` may run. Pure; see the module docstring
	for the full precedence contract.

	:param tool_name: catalog name of the tool, for the reason text only.
	:param risk: one of :data:`RISK_LEVELS`. ``None`` / anything else means
	        the tool is not in the catalog — denied.
	:param doctype: the doctype the call touches, or
	        :data:`TOOL_CATALOG_DOCTYPE` for tools that touch no document.
	:param policy_rules: raw rule dicts, most-preferred order irrelevant
	        (precedence is computed, not positional — position is only the
	        final tie-break).
	:param user_roles: the acting user's roles.
	:param amount: transaction value to test against the winning rule's
	        ``max_amount``. ``None`` means "not known" and fails closed when
	        a cap applies.
	"""
	tool_name = (tool_name or "").strip()
	requires_approval = risk in DEFAULT_DENY_RISKS

	if not tool_name:
		return PolicyDecision(
			False,
			"No tool was named in the request, so there is nothing to authorise.",
			False,
		)

	if risk not in RISK_LEVELS:
		return PolicyDecision(
			False,
			f"'{tool_name}' is not a tool in the agent catalog (no known risk level), so it is denied.",
			False,
		)

	doctype = (doctype or "").strip()
	if not doctype:
		return PolicyDecision(
			False,
			f"Tool '{tool_name}' did not identify a doctype, so {risk} access cannot be evaluated and is denied.",
			requires_approval,
		)

	rules = normalize_rules(policy_rules)
	roles = {role for role in (user_roles or []) if role}

	candidates = _matching_rules(rules, risk, doctype, roles)
	if not candidates:
		return PolicyDecision(
			False,
			(
				f"No policy rule grants {risk} on {doctype} to your roles, "
				f"so '{tool_name}' is denied by default."
			),
			requires_approval,
		)

	winner = candidates[0][3]

	if not winner["allow"]:
		return PolicyDecision(
			False,
			(
				f"Policy rule '{describe_rule(winner)}' explicitly denies {risk} on {doctype}; "
				f"'{tool_name}' is refused."
			),
			requires_approval,
			winner,
		)

	cap = winner["max_amount"]
	if cap is not None:
		value = _as_amount(amount)
		if value is None:
			return PolicyDecision(
				False,
				(
					f"Policy rule '{describe_rule(winner)}' caps {risk} on {doctype} at {cap:g}, but the "
					f"request carries no verifiable amount, so '{tool_name}' is denied."
				),
				requires_approval,
				winner,
			)
		if value > cap:
			return PolicyDecision(
				False,
				(
					f"Amount {value:g} exceeds the {cap:g} limit set by policy rule "
					f"'{describe_rule(winner)}'; '{tool_name}' is denied."
				),
				requires_approval,
				winner,
			)

	suffix = " Approval is still required before it takes effect." if requires_approval else ""
	return PolicyDecision(
		True,
		f"Allowed by policy rule '{describe_rule(winner)}' for {risk} on {doctype}.{suffix}",
		requires_approval,
		winner,
	)


def summarize_policy(policy_rules: list[dict], user_roles: list[str]) -> dict:
	"""What the holder of ``user_roles`` may do under ``policy_rules``.

	Pure. Feeds ``api.get_policy_summary`` so an agent framework can put the
	*actual* boundary into its system prompt — the prompt then describes the
	policy instead of pretending to be it.
	"""
	rules = normalize_rules(policy_rules)
	roles = {role for role in (user_roles or []) if role}
	summary: dict = {"roles": sorted(roles), "risks": {}}

	for risk in RISK_LEVELS:
		applicable = [
			rule
			for rule in rules
			if rule["risk_level"] == risk and (not rule["role"] or rule["role"] in roles)
		]
		patterns = sorted(
			{r["doctype_pattern"] for r in applicable if r["doctype_pattern"] not in ("", WILDCARD)}
		)

		allowed, denied, capped = [], [], {}
		for pattern in patterns:
			decision = evaluate_policy("(policy summary)", risk, pattern, policy_rules, user_roles, amount=0)
			if decision.allowed:
				allowed.append(pattern)
				cap = (decision.matched_rule or {}).get("max_amount")
				if cap is not None:
					capped[pattern] = cap
			else:
				denied.append(pattern)

		wildcard_candidates = [
			(int(rule["allow"]), index, rule)
			for index, rule in enumerate(applicable)
			if rule["doctype_pattern"] == WILDCARD
		]
		wildcard_candidates.sort(key=lambda item: item[:2])
		wildcard = bool(wildcard_candidates[0][2]["allow"]) if wildcard_candidates else False

		summary["risks"][risk] = {
			"allowed_doctypes": allowed,
			"denied_doctypes": denied,
			"amount_limits": capped,
			"any_doctype_allowed": wildcard,
			"requires_approval": risk in DEFAULT_DENY_RISKS,
		}

	return summary


# ------------------------------------------------- parameter schema checking

#: python types accepted for each JSON-schema type name. ``bool`` is excluded
#: from the numeric types on purpose — ``True`` is an int in Python and a
#: silently accepted ``limit=True`` is exactly the kind of surprise a guard
#: layer must not have.
_TYPE_CHECKS = {
	"string": lambda v: isinstance(v, str),
	"integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
	"number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
	"boolean": lambda v: isinstance(v, bool),
	"object": lambda v: isinstance(v, dict),
	"array": lambda v: isinstance(v, list),
}


def _coerce(value, expected: str):
	"""Best-effort coercion of transport-flattened values.

	MCP/HTTP transports flatten everything to strings: ``limit`` arrives as
	``"20"`` and ``filters`` as a JSON string. Coercing here (rather than in
	each handler) keeps the handlers typed and keeps the coercion rules in
	one testable place. Returns the value unchanged when it cannot be coerced
	— the type check downstream then reports it.
	"""
	if expected in ("object", "array") and isinstance(value, str):
		try:
			decoded = json.loads(value)
		except (TypeError, ValueError):
			return value
		return decoded if _TYPE_CHECKS[expected](decoded) else value

	if expected in ("integer", "number") and isinstance(value, str):
		try:
			return int(value) if expected == "integer" else float(value)
		except (TypeError, ValueError):
			return value

	if expected == "boolean" and value in (0, 1, "0", "1"):
		return value in (1, "1")

	return value


def validate_params(schema: dict, params: dict | None) -> tuple[dict, list[str]]:
	"""Validate + coerce ``params`` against a small JSON-schema subset.

	Supported keywords: ``required``, ``properties`` with ``type``, ``enum``,
	``minimum``, ``maximum``, ``default``, ``clamp``, and
	``additionalProperties: False``.

	``clamp: True`` on a numeric property means an out-of-range value is
	pulled back to the bound instead of rejected — that is how ``limit`` is
	capped: an agent asking for 5000 rows gets 50, not an error, so the cap
	is a guardrail rather than a puzzle. Without ``clamp`` a bound violation
	is an error.

	``additionalProperties: False`` (the default for every tool in the
	catalog) is a guard in its own right: an agent cannot smuggle an extra
	key such as ``ignore_permissions`` through the tool boundary.

	:returns: ``(cleaned_params, errors)``. ``errors`` empty means valid;
	          ``cleaned_params`` is only meaningful when there are no errors.
	"""
	schema = schema or {}
	properties = schema.get("properties") or {}
	required = schema.get("required") or []
	allow_extra = schema.get("additionalProperties", False)

	if params is None:
		params = {}
	if not isinstance(params, dict):
		return {}, ["Parameters must be a JSON object."]

	errors: list[str] = []
	cleaned: dict = {}

	if not allow_extra:
		for key in params:
			if key not in properties:
				errors.append(f"Unknown parameter '{key}'.")

	for key in required:
		if params.get(key) in (None, ""):
			errors.append(f"Missing required parameter '{key}'.")

	for key, spec in properties.items():
		if key not in params or params[key] is None:
			if "default" in spec:
				cleaned[key] = spec["default"]
			continue

		value = params[key]
		expected = spec.get("type")
		if expected in _TYPE_CHECKS:
			value = _coerce(value, expected)
			if not _TYPE_CHECKS[expected](value):
				errors.append(
					f"Parameter '{key}' must be of type {expected}, got {type(params[key]).__name__}."
				)
				continue

		enum = spec.get("enum")
		if enum is not None and value not in enum:
			errors.append(f"Parameter '{key}' must be one of {', '.join(map(str, enum))}; got '{value}'.")
			continue

		if expected in ("integer", "number"):
			clamp = bool(spec.get("clamp"))
			minimum, maximum = spec.get("minimum"), spec.get("maximum")
			if minimum is not None and value < minimum:
				if clamp:
					value = minimum
				else:
					errors.append(f"Parameter '{key}' must be at least {minimum}; got {value}.")
					continue
			if maximum is not None and value > maximum:
				if clamp:
					value = maximum
				else:
					errors.append(f"Parameter '{key}' must be at most {maximum}; got {value}.")
					continue

		cleaned[key] = value

	return cleaned, errors


# ------------------------------------------------------- the shipped default

#: Doctypes an agent may READ out of the box. Curated rather than ``*``: a
#: read allowlist is the cheapest way to keep an agent away from Users, API
#: keys, Email Accounts, Notification Settings and every other doctype whose
#: rows are infrastructure, not business data. Frappe's own permission check
#: still runs on top of this — the allowlist can only narrow, never widen.
DEFAULT_READ_DOCTYPES = (
	# masters
	"Company",
	"Customer",
	"Supplier",
	"Item",
	"Item Group",
	"Item Price",
	"Price List",
	"Customer Group",
	"Supplier Group",
	"Territory",
	"Contact",
	"Address",
	"Warehouse",
	"UOM",
	"Currency",
	"Account",
	"Cost Center",
	"Project",
	"Employee",
	"Batch",
	"Serial No",
	"Bin",
	"Report",
	# transactions
	"Quotation",
	"Sales Order",
	"Sales Invoice",
	"Delivery Note",
	"Purchase Order",
	"Purchase Invoice",
	"Purchase Receipt",
	"Material Request",
	"Stock Entry",
	"Journal Entry",
	"Payment Entry",
	"Lead",
	"Opportunity",
	"Task",
	"Issue",
	"Timesheet",
	"GL Entry",
	"Stock Ledger Entry",
)

#: Doctypes an agent may create/update **as drafts**. Deliberately the
#: transaction doctypes only: a draft is reviewable and reversible, and none
#: of these has any effect on the ledger or on stock until a human submits it.
DEFAULT_DRAFT_WRITE_DOCTYPES = (
	"Sales Order",
	"Purchase Order",
	"Sales Invoice",
	"Purchase Invoice",
	"Material Request",
	"Journal Entry",
	"Quotation",
	"Lead",
	"Task",
	"Issue",
)


def _build_default_rules() -> list[dict]:
	rules: list[dict] = [
		{
			"risk_level": ToolRisk.READ,
			"doctype_pattern": TOOL_CATALOG_DOCTYPE,
			"allow": 1,
			"role": None,
			"max_amount": None,
			"notes": "Tool self-description (list_tools) — touches no document.",
		}
	]
	rules += [
		{
			"risk_level": ToolRisk.READ,
			"doctype_pattern": doctype,
			"allow": 1,
			"role": None,
			"max_amount": None,
			"notes": "Curated read allowlist; Frappe permissions still apply.",
		}
		for doctype in DEFAULT_READ_DOCTYPES
	]
	rules += [
		{
			"risk_level": ToolRisk.DRAFT_WRITE,
			"doctype_pattern": doctype,
			"allow": 1,
			"role": None,
			"max_amount": None,
			"notes": "Drafts only (docstatus 0); a human submits.",
		}
		for doctype in DEFAULT_DRAFT_WRITE_DOCTYPES
	]
	rules += [
		{
			"risk_level": ToolRisk.SUBMIT,
			"doctype_pattern": WILDCARD,
			"allow": 0,
			"role": None,
			"max_amount": None,
			"notes": "Submits belong to a human or a Workflow, never to an agent.",
		},
		{
			"risk_level": ToolRisk.DESTRUCTIVE,
			"doctype_pattern": WILDCARD,
			"allow": 0,
			"role": None,
			"max_amount": None,
			"notes": "No destructive tool is implemented; this rule keeps it that way.",
		},
	]
	return rules


#: The safe shipped default, installed as an **Agent Policy** document by
#: ``agent_policy.install_default_policy()`` and used verbatim as the
#: fallback when no enabled default policy exists on the site. Read is
#: allowlisted, draft writes are allowed on transaction doctypes with no
#: amount cap, and SUBMIT/DESTRUCTIVE carry explicit ``*`` denials so the
#: refusal is visible in the UI rather than merely implied by default-deny.
DEFAULT_POLICY_RULES: list[dict] = _build_default_rules()

DEFAULT_POLICY_NAME = "Default Agent Policy"
