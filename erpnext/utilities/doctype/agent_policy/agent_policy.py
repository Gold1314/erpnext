# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Agent Policy — the operator-editable form of the agent guardrail.

The rules live here as data, and ``erpnext.agent.policy.evaluate_policy``
reads them. That separation is the point of "guardrails as policy, not
prompt": changing what an agent may do is an audited edit to a tracked
document, not a change to a prompt string somewhere in a codebase.

A site normally has exactly one enabled policy flagged ``is_default`` —
that is the one the agent surface enforces. Others can be staged (enabled
0) or kept for reference. With no enabled default, the surface falls back to
``erpnext.agent.policy.DEFAULT_POLICY_RULES``, the same safe set this
doctype's :func:`install_default_policy` writes.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext.agent.policy import (
	DEFAULT_POLICY_NAME,
	DEFAULT_POLICY_RULES,
	RISK_LEVELS,
	TOOL_CATALOG_DOCTYPE,
	WILDCARD,
)


class AgentPolicy(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.utilities.doctype.agent_policy_rule.agent_policy_rule import AgentPolicyRule

		description: DF.SmallText | None
		enabled: DF.Check
		is_default: DF.Check
		policy_name: DF.Data
		rules: DF.Table[AgentPolicyRule]
	# end: auto-generated types

	def validate(self):
		self.validate_rules()
		self.validate_default()

	def validate_rules(self):
		for row in self.rules:
			if row.risk_level not in RISK_LEVELS:
				frappe.throw(
					_("Row {0}: {1} is not a known risk level.").format(row.idx, row.risk_level),
					title=_("Invalid Rule"),
				)

			pattern = (row.doctype_pattern or "").strip()
			if not pattern:
				frappe.throw(_("Row {0}: a doctype pattern is required (use * for any).").format(row.idx))

			row.doctype_pattern = pattern
			if pattern not in (WILDCARD, TOOL_CATALOG_DOCTYPE) and not frappe.db.exists("DocType", pattern):
				frappe.throw(
					_("Row {0}: {1} is not a doctype on this site. Use an exact doctype name or *.").format(
						row.idx, pattern
					),
					title=_("Invalid Rule"),
				)

			if flt(row.max_amount) < 0:
				frappe.throw(_("Row {0}: Max Amount cannot be negative.").format(row.idx))

	def validate_default(self):
		"""Exactly one enabled default. A disabled policy cannot be the default —
		otherwise the surface would silently fall back to the shipped rules while
		the UI still showed this document as the one in force."""
		if not self.is_default:
			return

		if not self.enabled:
			frappe.throw(_("A disabled policy cannot be the default. Enable it, or clear Is Default."))

		others = frappe.get_all(
			"Agent Policy",
			filters={"is_default": 1, "name": ("!=", self.name)},
			pluck="name",
		)
		for name in others:
			frappe.db.set_value("Agent Policy", name, "is_default", 0)

		if others:
			frappe.msgprint(
				_("{0} is now the default agent policy; {1} no longer is.").format(
					self.name, ", ".join(others)
				),
				indicator="blue",
			)


@frappe.whitelist()
def install_default_policy():
	"""Create the shipped safe default policy from ``DEFAULT_POLICY_RULES``.

	Read is allowlisted to a curated set of business doctypes, draft writes
	are allowed on transaction doctypes with no amount cap, and SUBMIT and
	DESTRUCTIVE carry explicit ``*`` denials so the refusal is visible in the
	UI rather than only implied by default-deny.

	Idempotent: an existing policy of the same name is left untouched, because
	overwriting it would silently discard an operator's edits.
	"""
	frappe.only_for("System Manager")

	if frappe.db.exists("Agent Policy", DEFAULT_POLICY_NAME):
		return {
			"policy": DEFAULT_POLICY_NAME,
			"status": "skipped",
			"message": _("{0} already exists; it was left unchanged.").format(DEFAULT_POLICY_NAME),
		}

	# do not steal the default flag from a policy an operator already runs
	existing_default = frappe.db.exists("Agent Policy", {"is_default": 1, "enabled": 1})

	# every doctype in the shipped list is part of ERPNext, but a stripped or
	# customised build may not carry all of them — skip what is absent rather
	# than failing the whole install on one missing doctype
	rules = [
		dict(rule)
		for rule in DEFAULT_POLICY_RULES
		if rule["doctype_pattern"] in (WILDCARD, TOOL_CATALOG_DOCTYPE)
		or frappe.db.exists("DocType", rule["doctype_pattern"])
	]

	doc = frappe.get_doc(
		{
			"doctype": "Agent Policy",
			"policy_name": DEFAULT_POLICY_NAME,
			"enabled": 1,
			"is_default": 0 if existing_default else 1,
			"description": _(
				"Shipped default: curated read allowlist, drafts only on transaction doctypes, "
				"submit and destructive actions denied everywhere."
			),
			"rules": rules,
		}
	)
	doc.insert()

	return {
		"policy": doc.name,
		"status": "created",
		"is_default": bool(doc.is_default),
		"rules": len(doc.rules),
		"message": (
			_("{0} created and set as the default agent policy.").format(doc.name)
			if doc.is_default
			else _("{0} created. {1} is still the default — switch it over manually.").format(
				doc.name, existing_default
			)
		),
	}
