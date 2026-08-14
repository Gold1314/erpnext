# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class SoDRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.sod_rule_capability.sod_rule_capability import SoDRuleCapability

		capabilities: DF.Table[SoDRuleCapability]
		description: DF.SmallText | None
		enabled: DF.Check
		risk_level: DF.Literal["High", "Medium", "Low"]
		rule_name: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_capabilities()

	def validate_capabilities(self):
		sides = {row.side for row in self.capabilities}
		if self.enabled and ("First Function" not in sides or "Second Function" not in sides):
			frappe.throw(
				_(
					"An enabled SoD Rule requires at least one role on the First Function side and one on the Second Function side."
				)
			)


@frappe.whitelist()
def scan_for_violations():
	"""Whitelisted wrapper so the scan can be triggered from the UI."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	return run_sod_scan()


def run_sod_scan():
	"""Cross-check role assignments of active System Users against enabled SoD Rules.

	For every user x rule combination, the rule is violated when the user holds
	at least one role from the rule's "First Function" side and at least one
	role from its "Second Function" side. Violations are upserted into
	`SoD Violation Log`; Open logs whose conflict has disappeared are marked
	Resolved.
	"""
	rules = get_enabled_rules()
	user_roles = get_user_role_map()

	scan_time = now_datetime()
	matched_pairs = set()
	new_violations = 0
	refreshed_violations = 0

	for rule in rules:
		first_side = {row.role for row in rule.capabilities if row.side == "First Function"}
		second_side = {row.role for row in rule.capabilities if row.side == "Second Function"}
		if not first_side or not second_side:
			continue

		for user, roles in user_roles.items():
			first_matches = roles & first_side
			second_matches = roles & second_side
			if not (first_matches and second_matches):
				continue

			matched_pairs.add((user, rule.name))
			if upsert_violation_log(user, rule, first_matches, second_matches, scan_time):
				new_violations += 1
			else:
				refreshed_violations += 1

	resolved_violations = resolve_stale_violations(matched_pairs, scan_time)

	return {
		"rules_scanned": len(rules),
		"users_scanned": len(user_roles),
		"new_violations": new_violations,
		"refreshed_violations": refreshed_violations,
		"resolved_violations": resolved_violations,
	}


def get_enabled_rules():
	rule_names = frappe.get_all("SoD Rule", filters={"enabled": 1}, pluck="name")
	return [frappe.get_doc("SoD Rule", name) for name in rule_names]


def get_user_role_map():
	"""Return {user: set(roles)} for enabled System Users (excluding Administrator and Guest)."""
	users = frappe.get_all(
		"User",
		filters={
			"enabled": 1,
			"user_type": "System User",
			"name": ("not in", ("Administrator", "Guest")),
		},
		pluck="name",
	)

	user_roles = {user: set() for user in users}
	for has_role in frappe.get_all("Has Role", filters={"parenttype": "User"}, fields=["parent", "role"]):
		if has_role.parent in user_roles and has_role.role:
			user_roles[has_role.parent].add(has_role.role)

	return user_roles


def upsert_violation_log(user, rule, first_matches, second_matches, scan_time):
	"""Create an Open violation log, or refresh the existing Open one for the same user + rule.

	Returns True when a new log was created, False when an existing one was refreshed.
	"""
	first_function_roles = ", ".join(sorted(first_matches))
	second_function_roles = ", ".join(sorted(second_matches))

	existing = frappe.db.get_value(
		"SoD Violation Log",
		{"user": user, "sod_rule": rule.name, "status": "Open"},
		"name",
	)

	if existing:
		log = frappe.get_doc("SoD Violation Log", existing)
		log.first_function_roles = first_function_roles
		log.second_function_roles = second_function_roles
		log.detected_on = scan_time
		log.flags.via_sod_scan = True
		log.save(ignore_permissions=True)
		return False

	frappe.get_doc(
		{
			"doctype": "SoD Violation Log",
			"user": user,
			"sod_rule": rule.name,
			"risk_level": rule.risk_level,
			"first_function_roles": first_function_roles,
			"second_function_roles": second_function_roles,
			"detected_on": scan_time,
			"status": "Open",
		}
	).insert(ignore_permissions=True)
	return True


def resolve_stale_violations(matched_pairs, scan_time):
	"""Mark Open violation logs whose conflict no longer exists as Resolved."""
	resolved = 0
	for log in frappe.get_all(
		"SoD Violation Log", filters={"status": "Open"}, fields=["name", "user", "sod_rule"]
	):
		if (log.user, log.sod_rule) in matched_pairs:
			continue

		doc = frappe.get_doc("SoD Violation Log", log.name)
		doc.status = "Resolved"
		doc.resolved_by = frappe.session.user
		doc.resolved_on = scan_time
		doc.flags.via_sod_scan = True
		doc.save(ignore_permissions=True)
		resolved += 1

	return resolved


@frappe.whitelist()
def create_default_sod_rules():
	"""Idempotently install a pack of classic segregation-of-duties conflicts."""
	frappe.only_for(("System Manager", "Accounts Manager"))

	created = []
	skipped = []
	for rule in get_default_sod_rules():
		if frappe.db.exists("SoD Rule", rule["rule_name"]):
			skipped.append(rule["rule_name"])
			continue

		doc = frappe.new_doc("SoD Rule")
		doc.rule_name = rule["rule_name"]
		doc.description = rule["description"]
		doc.risk_level = rule["risk_level"]
		doc.enabled = 1
		for role in rule["first_function"]:
			doc.append("capabilities", {"side": "First Function", "role": role})
		for role in rule["second_function"]:
			doc.append("capabilities", {"side": "Second Function", "role": role})
		doc.insert(ignore_permissions=True)
		created.append(rule["rule_name"])

	return {"created": created, "skipped": skipped}


def get_default_sod_rules():
	"""Rule pack built only from roles that ship with ERPNext."""
	return [
		{
			"rule_name": "Supplier Master vs Payment Processing",
			"description": "Users who maintain the supplier master and also post payments can create and pay fictitious suppliers.",
			"risk_level": "High",
			"first_function": ["Purchase Master Manager"],
			"second_function": ["Accounts User"],
		},
		{
			"rule_name": "Purchase Order Entry vs Invoice Approval",
			"description": "Users who create purchase orders and also approve purchase invoices can push unauthorized spend through to payment.",
			"risk_level": "High",
			"first_function": ["Purchase User"],
			"second_function": ["Accounts Manager"],
		},
		{
			"rule_name": "Customer Master vs Receivables Posting",
			"description": "Users who maintain the customer master and also post receivable entries can conceal lapping or divert receipts.",
			"risk_level": "High",
			"first_function": ["Sales Master Manager"],
			"second_function": ["Accounts User"],
		},
		{
			"rule_name": "Invoice Entry vs Payment Approval",
			"description": "Users who enter supplier invoices and also approve or post payments control the full payables cycle end to end.",
			"risk_level": "High",
			"first_function": ["Accounts User"],
			"second_function": ["Accounts Manager"],
		},
		{
			"rule_name": "Item Master vs Stock Adjustment",
			"description": "Users who maintain item valuation data and also reconcile or adjust stock can hide shrinkage and manipulate inventory value.",
			"risk_level": "High",
			"first_function": ["Item Manager"],
			"second_function": ["Stock Manager"],
		},
		{
			"rule_name": "System Administration vs Financial Posting",
			"description": "Users with full system administration rights who also post financial entries can alter controls around their own transactions.",
			"risk_level": "High",
			"first_function": ["System Manager"],
			"second_function": ["Accounts User", "Accounts Manager"],
		},
		{
			"rule_name": "Audit vs Financial Posting",
			"description": "Auditors who can also post financial entries lose independence over the records they review.",
			"risk_level": "High",
			"first_function": ["Auditor"],
			"second_function": ["Accounts User", "Accounts Manager"],
		},
		{
			"rule_name": "Item and Price Master vs Sales Entry",
			"description": "Users who maintain items and selling prices and also record sales can grant themselves unapproved discounts.",
			"risk_level": "Medium",
			"first_function": ["Item Manager"],
			"second_function": ["Sales User"],
		},
		{
			"rule_name": "Stock Entry vs Stock Reconciliation",
			"description": "Users who post stock movements and also approve stock reconciliations can cover up inventory discrepancies they created.",
			"risk_level": "Medium",
			"first_function": ["Stock User"],
			"second_function": ["Stock Manager"],
		},
		{
			"rule_name": "Purchase Ordering vs Goods Receipt",
			"description": "Users who create purchase orders and also post goods receipts can confirm delivery of goods that never arrived.",
			"risk_level": "Medium",
			"first_function": ["Purchase User"],
			"second_function": ["Stock User"],
		},
		{
			"rule_name": "Purchase Approval vs Supplier Master",
			"description": "Users who approve purchases and also maintain the supplier master can steer approved spend to suppliers they control.",
			"risk_level": "Medium",
			"first_function": ["Purchase Manager"],
			"second_function": ["Purchase Master Manager"],
		},
		{
			"rule_name": "Sales Ordering vs Goods Dispatch",
			"description": "Users who create sales orders and also post deliveries can ship goods without an approved commercial basis.",
			"risk_level": "Low",
			"first_function": ["Sales User"],
			"second_function": ["Stock User"],
		},
	]
