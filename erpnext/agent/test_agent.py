# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the agent policy engine, parameter validator and redactor.

The policy engine is a **security boundary**, so it is tested the way a
security boundary should be: exhaustively, on plain dicts, with no site, no
database and no network.

``policy.py`` has no frappe dependency at all, but importing it through the
``erpnext`` package would pull in ``erpnext/__init__.py`` (which imports
frappe). So when run as a plain file -

	python erpnext/agent/test_agent.py

- the module is loaded directly from its path under its canonical name (same
bootstrap as erpnext/accounts/anomaly/test_anomaly.py). ``audit.py`` does
``import frappe`` at module level for its log writer; the redaction helpers
next to it are pure, so a stub frappe module is installed just far enough to
let the import succeed. Nothing in these tests calls anything that touches
the stub.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar

_HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


try:
	from erpnext.agent import audit, policy
except Exception:
	for _pkg in ("erpnext", "erpnext.agent"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	if "frappe" not in sys.modules:
		_frappe = types.ModuleType("frappe")
		_frappe.session = types.SimpleNamespace(user="Administrator")
		sys.modules["frappe"] = _frappe

	policy = _load_module("erpnext.agent.policy", _HERE / "policy.py")
	audit = _load_module("erpnext.agent.audit", _HERE / "audit.py")


READ = policy.ToolRisk.READ
DRAFT_WRITE = policy.ToolRisk.DRAFT_WRITE
SUBMIT = policy.ToolRisk.SUBMIT
DESTRUCTIVE = policy.ToolRisk.DESTRUCTIVE


def rule(risk, pattern, allow=1, role=None, max_amount=None, notes=None):
	return {
		"risk_level": risk,
		"doctype_pattern": pattern,
		"allow": allow,
		"role": role,
		"max_amount": max_amount,
		"notes": notes,
	}


class TestDefaultDeny(unittest.TestCase):
	def test_no_rules_denies_every_risk(self):
		for risk in policy.RISK_LEVELS:
			decision = policy.evaluate_policy("some_tool", risk, "Sales Order", [], ["Sales User"])
			self.assertFalse(decision.allowed, risk)
			self.assertIn("default", decision.reason.lower())

	def test_submit_denied_without_explicit_rule_even_when_read_is_open(self):
		rules = [rule(READ, policy.WILDCARD), rule(DRAFT_WRITE, policy.WILDCARD)]
		decision = policy.evaluate_policy(
			"submit_document", SUBMIT, "Sales Invoice", rules, ["Accounts User"]
		)
		self.assertFalse(decision.allowed)
		self.assertTrue(decision.requires_approval)

	def test_destructive_denied_without_explicit_rule(self):
		rules = [rule(READ, policy.WILDCARD), rule(DRAFT_WRITE, policy.WILDCARD)]
		decision = policy.evaluate_policy(
			"delete_document", DESTRUCTIVE, "Sales Order", rules, ["System Manager"]
		)
		self.assertFalse(decision.allowed)
		self.assertTrue(decision.requires_approval)

	def test_requires_approval_flag_tracks_risk_class_not_outcome(self):
		rules = [rule(SUBMIT, "Sales Order")]
		allowed = policy.evaluate_policy("submit_document", SUBMIT, "Sales Order", rules, [])
		self.assertTrue(allowed.allowed)
		self.assertTrue(allowed.requires_approval, "an allowed SUBMIT still needs human/workflow approval")

		read = policy.evaluate_policy("get_document", READ, "Sales Order", [rule(READ, "Sales Order")], [])
		self.assertTrue(read.allowed)
		self.assertFalse(read.requires_approval)


class TestExplicitAllow(unittest.TestCase):
	def test_explicit_allow_permits(self):
		rules = [rule(DRAFT_WRITE, "Sales Order")]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, ["Sales User"])
		self.assertTrue(decision.allowed)
		self.assertIn("Sales Order", decision.reason)

	def test_explicit_allow_of_submit_permits_when_operator_opts_in(self):
		rules = [rule(SUBMIT, "Sales Order", allow=1, role="Sales Manager")]
		decision = policy.evaluate_policy(
			"submit_document", SUBMIT, "Sales Order", rules, ["Sales Manager"], amount=999999
		)
		self.assertTrue(decision.allowed)
		self.assertTrue(decision.requires_approval)

	def test_allow_on_one_doctype_does_not_leak_to_another(self):
		rules = [rule(DRAFT_WRITE, "Sales Order")]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Journal Entry", rules, [])
		self.assertFalse(decision.allowed)
		self.assertIn("Journal Entry", decision.reason)

	def test_risk_classes_do_not_leak_into_each_other(self):
		rules = [rule(READ, policy.WILDCARD)]
		self.assertTrue(policy.evaluate_policy("get_document", READ, "Item", rules, []).allowed)
		self.assertFalse(policy.evaluate_policy("create_draft", DRAFT_WRITE, "Item", rules, []).allowed)


class TestPrecedence(unittest.TestCase):
	def test_exact_doctype_beats_wildcard_allow(self):
		rules = [rule(DRAFT_WRITE, policy.WILDCARD, allow=1), rule(DRAFT_WRITE, "Journal Entry", allow=0)]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Journal Entry", rules, [])
		self.assertFalse(decision.allowed)
		self.assertIn("explicitly denies", decision.reason)

		other = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, [])
		self.assertTrue(other.allowed)

	def test_exact_doctype_beats_wildcard_deny(self):
		rules = [rule(SUBMIT, policy.WILDCARD, allow=0), rule(SUBMIT, "Task", allow=1)]
		self.assertTrue(policy.evaluate_policy("submit_document", SUBMIT, "Task", rules, []).allowed)
		self.assertFalse(policy.evaluate_policy("submit_document", SUBMIT, "Sales Order", rules, []).allowed)

	def test_exact_without_role_outranks_wildcard_with_role(self):
		"""The doctype axis dominates the role axis, as specified."""
		rules = [
			rule(DRAFT_WRITE, policy.WILDCARD, allow=1, role="Sales User"),
			rule(DRAFT_WRITE, "Journal Entry", allow=0),
		]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Journal Entry", rules, ["Sales User"])
		self.assertFalse(decision.allowed)

	def test_role_scoped_rule_beats_unscoped_rule_at_same_doctype(self):
		rules = [
			rule(DRAFT_WRITE, "Sales Order", allow=0),
			rule(DRAFT_WRITE, "Sales Order", allow=1, role="Sales Manager"),
		]
		self.assertTrue(
			policy.evaluate_policy(
				"create_draft", DRAFT_WRITE, "Sales Order", rules, ["Sales Manager"]
			).allowed
		)
		self.assertFalse(
			policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, ["Sales User"]).allowed
		)

	def test_deny_wins_ties_at_equal_specificity(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1), rule(DRAFT_WRITE, "Sales Order", allow=0)]
		self.assertFalse(
			policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, []).allowed
		)

		# and the same the other way round: order of declaration must not matter
		reversed_rules = list(reversed(rules))
		self.assertFalse(
			policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", reversed_rules, []).allowed
		)

	def test_decision_is_deterministic_across_rule_orderings(self):
		rules = [
			rule(READ, policy.WILDCARD, allow=1),
			rule(READ, "Item", allow=0),
			rule(READ, "Item", allow=1, role="Item Manager"),
		]
		first = policy.evaluate_policy("get_document", READ, "Item", rules, ["Item Manager"])
		second = policy.evaluate_policy("get_document", READ, "Item", list(reversed(rules)), ["Item Manager"])
		self.assertEqual(first.allowed, second.allowed)
		self.assertEqual(first.reason, second.reason)

	def test_winning_rule_does_not_fall_through_when_it_denies(self):
		"""An exact deny is final — evaluation does not go looking for a laxer
		wildcard allow underneath it."""
		rules = [rule(READ, "Item", allow=0), rule(READ, policy.WILDCARD, allow=1)]
		self.assertFalse(policy.evaluate_policy("get_document", READ, "Item", rules, []).allowed)


class TestRoleGating(unittest.TestCase):
	def test_rule_ignored_when_user_lacks_its_role(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, role="Sales Manager")]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, ["Sales User"])
		self.assertFalse(decision.allowed)
		self.assertIn("No policy rule grants", decision.reason)

	def test_rule_applies_when_user_holds_its_role(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, role="Sales Manager")]
		decision = policy.evaluate_policy(
			"create_draft", DRAFT_WRITE, "Sales Order", rules, ["Sales User", "Sales Manager"]
		)
		self.assertTrue(decision.allowed)

	def test_unscoped_rule_applies_to_everyone(self):
		rules = [rule(READ, "Item")]
		self.assertTrue(policy.evaluate_policy("get_document", READ, "Item", rules, []).allowed)
		self.assertTrue(policy.evaluate_policy("get_document", READ, "Item", rules, ["Guest"]).allowed)

	def test_role_scoped_deny_does_not_affect_other_users(self):
		rules = [rule(READ, "Item", allow=1), rule(READ, "Item", allow=0, role="Restricted Agent")]
		self.assertFalse(
			policy.evaluate_policy("get_document", READ, "Item", rules, ["Restricted Agent"]).allowed
		)
		self.assertTrue(policy.evaluate_policy("get_document", READ, "Item", rules, ["Item Manager"]).allowed)


class TestAmountCap(unittest.TestCase):
	def test_amount_equal_to_cap_is_allowed(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=10000)]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=10000)
		self.assertTrue(decision.allowed)

	def test_amount_over_cap_is_denied_with_both_numbers_in_the_reason(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=10000)]
		decision = policy.evaluate_policy(
			"create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=10000.01
		)
		self.assertFalse(decision.allowed)
		self.assertIn("10000", decision.reason)
		self.assertIn("exceeds", decision.reason)

	def test_amount_just_under_cap_is_allowed(self):
		rules = [rule(SUBMIT, "Purchase Order", allow=1, max_amount=5000)]
		self.assertTrue(
			policy.evaluate_policy(
				"submit_document", SUBMIT, "Purchase Order", rules, [], amount=4999.99
			).allowed
		)

	def test_none_max_amount_is_unlimited(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=None)]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=10**9)
		self.assertTrue(decision.allowed)

	def test_zero_max_amount_is_unlimited_because_frappe_stores_blank_currency_as_zero(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=0)]
		decision = policy.evaluate_policy(
			"create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=750000
		)
		self.assertTrue(decision.allowed)
		self.assertIsNone(policy.normalize_rule(rules[0])["max_amount"])

	def test_unknown_amount_against_a_cap_fails_closed(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=10000)]
		decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=None)
		self.assertFalse(decision.allowed)
		self.assertIn("no verifiable amount", decision.reason)

	def test_uncoercible_amount_against_a_cap_fails_closed(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=10000)]
		decision = policy.evaluate_policy(
			"create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount="not-a-number"
		)
		self.assertFalse(decision.allowed)

	def test_amount_as_string_is_coerced(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, max_amount=10000)]
		self.assertTrue(
			policy.evaluate_policy(
				"create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount="9500"
			).allowed
		)
		self.assertFalse(
			policy.evaluate_policy(
				"create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount="19500"
			).allowed
		)

	def test_no_cap_means_amount_is_irrelevant(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1)]
		self.assertTrue(
			policy.evaluate_policy("create_draft", DRAFT_WRITE, "Sales Order", rules, [], amount=None).allowed
		)


class TestUnknownInputs(unittest.TestCase):
	def test_unknown_tool_is_denied(self):
		"""``tools._guarded`` passes ``risk=None`` for a name that is not in the
		catalog; that must resolve to deny, not to a crash."""
		decision = policy.evaluate_policy(
			"drop_all_tables", None, "Sales Order", policy.DEFAULT_POLICY_RULES, []
		)
		self.assertFalse(decision.allowed)
		self.assertIn("not a tool in the agent catalog", decision.reason)

	def test_unrecognised_risk_string_is_denied(self):
		decision = policy.evaluate_policy("weird_tool", "SUPER_ADMIN", "Item", [rule("SUPER_ADMIN", "*")], [])
		self.assertFalse(decision.allowed)

	def test_empty_tool_name_is_denied(self):
		decision = policy.evaluate_policy("", READ, "Item", [rule(READ, "*")], [])
		self.assertFalse(decision.allowed)
		self.assertTrue(decision.reason)

	def test_unknown_doctype_is_denied_under_the_default_policy(self):
		decision = policy.evaluate_policy(
			"get_document", READ, "User", policy.DEFAULT_POLICY_RULES, ["System Manager"]
		)
		self.assertFalse(decision.allowed)
		self.assertIn("User", decision.reason)

	def test_missing_doctype_is_denied(self):
		decision = policy.evaluate_policy("get_document", READ, None, [rule(READ, "*")], [])
		self.assertFalse(decision.allowed)
		self.assertIn("did not identify a doctype", decision.reason)

	def test_every_decision_carries_a_non_empty_reason(self):
		cases = [
			("t", READ, "Item", [], []),
			("t", READ, "Item", [rule(READ, "Item")], []),
			("t", SUBMIT, "Item", [rule(SUBMIT, "Item", allow=0)], []),
			("t", None, "Item", [], []),
			("", READ, "Item", [], []),
			("t", READ, None, [], []),
			("t", DRAFT_WRITE, "Item", [rule(DRAFT_WRITE, "Item", max_amount=5)], []),
		]
		for args in cases:
			decision = policy.evaluate_policy(*args)
			self.assertIsInstance(decision.reason, str)
			self.assertTrue(decision.reason.strip(), args)
			self.assertIsInstance(decision.allowed, bool)


class TestDefaultPolicyRules(unittest.TestCase):
	def setUp(self):
		self.rules = policy.DEFAULT_POLICY_RULES
		self.roles = ["System Manager", "Accounts Manager", "Sales User"]

	def test_submit_is_denied_for_every_doctype_tested(self):
		for doctype in (
			*policy.DEFAULT_DRAFT_WRITE_DOCTYPES,
			*policy.DEFAULT_READ_DOCTYPES,
			"Payment Entry",
			"Delivery Note",
			"Some Custom Doctype",
		):
			decision = policy.evaluate_policy(
				"submit_document", SUBMIT, doctype, self.rules, self.roles, amount=1
			)
			self.assertFalse(decision.allowed, f"SUBMIT must be denied on {doctype}")
			self.assertTrue(decision.requires_approval)

	def test_destructive_is_denied_everywhere(self):
		for doctype in ("Sales Order", "Journal Entry", "Item", "Anything At All"):
			decision = policy.evaluate_policy("delete_document", DESTRUCTIVE, doctype, self.rules, self.roles)
			self.assertFalse(decision.allowed, doctype)

	def test_draft_write_allowed_on_every_listed_transaction_doctype(self):
		for doctype in policy.DEFAULT_DRAFT_WRITE_DOCTYPES:
			decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, doctype, self.rules, self.roles)
			self.assertTrue(decision.allowed, f"DRAFT_WRITE must be allowed on {doctype}")
			self.assertFalse(decision.requires_approval)

	def test_draft_write_denied_outside_the_transaction_list(self):
		for doctype in ("Item", "Customer", "User", "Company", "Payment Entry"):
			decision = policy.evaluate_policy("create_draft", DRAFT_WRITE, doctype, self.rules, self.roles)
			self.assertFalse(decision.allowed, f"DRAFT_WRITE must be denied on {doctype}")

	def test_read_allowed_on_every_curated_doctype(self):
		for doctype in policy.DEFAULT_READ_DOCTYPES:
			decision = policy.evaluate_policy("search_documents", READ, doctype, self.rules, self.roles)
			self.assertTrue(decision.allowed, f"READ must be allowed on {doctype}")

	def test_read_denied_on_infrastructure_doctypes(self):
		for doctype in ("User", "Role", "Workflow", "Email Account", "Server Script", "System Settings"):
			decision = policy.evaluate_policy("search_documents", READ, doctype, self.rules, self.roles)
			self.assertFalse(decision.allowed, f"READ must be denied on {doctype}")

	def test_draft_write_rules_carry_no_amount_cap(self):
		for row in policy.normalize_rules(self.rules):
			if row["risk_level"] == DRAFT_WRITE:
				self.assertIsNone(row["max_amount"], row["doctype_pattern"])

	def test_tool_catalog_pseudo_doctype_is_readable(self):
		decision = policy.evaluate_policy("list_tools", READ, policy.TOOL_CATALOG_DOCTYPE, self.rules, [])
		self.assertTrue(decision.allowed)

	def test_shipped_rules_include_explicit_wildcard_denials(self):
		normalized = policy.normalize_rules(self.rules)
		for risk in (SUBMIT, DESTRUCTIVE):
			matches = [
				row
				for row in normalized
				if row["risk_level"] == risk
				and row["doctype_pattern"] == policy.WILDCARD
				and not row["allow"]
			]
			self.assertEqual(len(matches), 1, risk)

	def test_shipped_rules_are_all_well_formed(self):
		for row in policy.normalize_rules(self.rules):
			self.assertIn(row["risk_level"], policy.RISK_LEVELS)
			self.assertTrue(row["doctype_pattern"])
			self.assertIsInstance(row["allow"], bool)


class TestNormalizeRule(unittest.TestCase):
	def test_string_zero_is_a_denial(self):
		self.assertFalse(policy.normalize_rule({"allow": "0"})["allow"])
		self.assertFalse(policy.normalize_rule({"allow": ""})["allow"])
		self.assertFalse(policy.normalize_rule({"allow": 0})["allow"])
		self.assertTrue(policy.normalize_rule({"allow": "1"})["allow"])
		self.assertTrue(policy.normalize_rule({"allow": 1})["allow"])
		self.assertTrue(policy.normalize_rule({"allow": True})["allow"])

	def test_blank_role_normalizes_to_none(self):
		self.assertIsNone(policy.normalize_rule({"role": "  "})["role"])
		self.assertEqual(policy.normalize_rule({"role": " Sales User "})["role"], "Sales User")

	def test_patterns_are_stripped(self):
		self.assertEqual(
			policy.normalize_rule({"doctype_pattern": " Sales Order "})["doctype_pattern"], "Sales Order"
		)


class TestSummarizePolicy(unittest.TestCase):
	def test_default_summary_matches_the_shipped_intent(self):
		summary = policy.summarize_policy(policy.DEFAULT_POLICY_RULES, ["Sales User"])

		self.assertIn("Sales Order", summary["risks"][READ]["allowed_doctypes"])
		self.assertEqual(
			sorted(summary["risks"][DRAFT_WRITE]["allowed_doctypes"]),
			sorted(policy.DEFAULT_DRAFT_WRITE_DOCTYPES),
		)
		self.assertEqual(summary["risks"][SUBMIT]["allowed_doctypes"], [])
		self.assertFalse(summary["risks"][SUBMIT]["any_doctype_allowed"])
		self.assertFalse(summary["risks"][DESTRUCTIVE]["any_doctype_allowed"])
		self.assertTrue(summary["risks"][SUBMIT]["requires_approval"])

	def test_summary_respects_role_gating(self):
		rules = [rule(DRAFT_WRITE, "Sales Order", allow=1, role="Sales Manager")]
		manager = policy.summarize_policy(rules, ["Sales Manager"])
		clerk = policy.summarize_policy(rules, ["Sales User"])

		self.assertEqual(manager["risks"][DRAFT_WRITE]["allowed_doctypes"], ["Sales Order"])
		self.assertEqual(clerk["risks"][DRAFT_WRITE]["allowed_doctypes"], [])

	def test_summary_reports_amount_limits_and_wildcards(self):
		rules = [rule(READ, policy.WILDCARD, allow=1), rule(DRAFT_WRITE, "Task", allow=1, max_amount=250)]
		summary = policy.summarize_policy(rules, [])
		self.assertTrue(summary["risks"][READ]["any_doctype_allowed"])
		self.assertEqual(summary["risks"][DRAFT_WRITE]["amount_limits"], {"Task": 250.0})


class TestValidateParams(unittest.TestCase):
	SCHEMA: ClassVar[dict] = {
		"type": "object",
		"required": ["doctype"],
		"additionalProperties": False,
		"properties": {
			"doctype": {"type": "string"},
			"filters": {"type": "object"},
			"fields": {"type": "array"},
			"limit": {"type": "integer", "minimum": 1, "maximum": 50, "clamp": True, "default": 20},
			"strict_bound": {"type": "integer", "minimum": 0, "maximum": 10},
			"mode": {"type": "string", "enum": ["draft", "final"]},
			"flag": {"type": "boolean"},
		},
	}

	def test_valid_params_pass_and_defaults_apply(self):
		cleaned, errors = validate = policy.validate_params(self.SCHEMA, {"doctype": "Item"})
		self.assertEqual(errors, [])
		self.assertEqual(cleaned["doctype"], "Item")
		self.assertEqual(cleaned["limit"], 20)
		self.assertEqual(len(validate), 2)

	def test_missing_required_key_is_reported(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"limit": 5})
		self.assertTrue(any("Missing required parameter 'doctype'" in e for e in errors))

	def test_blank_required_key_is_reported(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": ""})
		self.assertTrue(any("doctype" in e for e in errors))

	def test_wrong_type_is_reported(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "filters": ["a", "b"]})
		self.assertTrue(any("'filters' must be of type object" in e for e in errors), errors)

	def test_boolean_is_not_accepted_as_an_integer(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "limit": True})
		self.assertTrue(any("'limit' must be of type integer" in e for e in errors), errors)

	def test_enum_violation_is_reported(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "mode": "submitted"})
		self.assertTrue(any("must be one of" in e for e in errors), errors)

	def test_enum_value_passes(self):
		cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "mode": "draft"})
		self.assertEqual(errors, [])
		self.assertEqual(cleaned["mode"], "draft")

	def test_limit_is_clamped_not_rejected(self):
		cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "limit": 5000})
		self.assertEqual(errors, [])
		self.assertEqual(cleaned["limit"], 50)

		cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "limit": 0})
		self.assertEqual(errors, [])
		self.assertEqual(cleaned["limit"], 1)

	def test_non_clamped_bound_violation_is_an_error(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "strict_bound": 99})
		self.assertTrue(any("at most 10" in e for e in errors), errors)

		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "strict_bound": -1})
		self.assertTrue(any("at least 0" in e for e in errors), errors)

	def test_unknown_parameter_is_rejected(self):
		_cleaned, errors = policy.validate_params(
			self.SCHEMA, {"doctype": "Item", "ignore_permissions": True}
		)
		self.assertTrue(any("Unknown parameter 'ignore_permissions'" in e for e in errors), errors)

	def test_transport_flattened_values_are_coerced(self):
		cleaned, errors = policy.validate_params(
			self.SCHEMA,
			{"doctype": "Item", "limit": "25", "filters": '{"item_group": "Products"}', "fields": '["name"]'},
		)
		self.assertEqual(errors, [])
		self.assertEqual(cleaned["limit"], 25)
		self.assertEqual(cleaned["filters"], {"item_group": "Products"})
		self.assertEqual(cleaned["fields"], ["name"])

	def test_uncoercible_string_still_fails_the_type_check(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, {"doctype": "Item", "limit": "twenty"})
		self.assertTrue(any("'limit' must be of type integer" in e for e in errors), errors)

	def test_non_object_params_are_rejected(self):
		_cleaned, errors = policy.validate_params(self.SCHEMA, ["doctype", "Item"])
		self.assertEqual(errors, ["Parameters must be a JSON object."])

	def test_empty_schema_accepts_nothing_extra(self):
		schema = {"type": "object", "required": [], "additionalProperties": False, "properties": {}}
		cleaned, errors = policy.validate_params(schema, {})
		self.assertEqual((cleaned, errors), ({}, []))

		_cleaned, errors = policy.validate_params(schema, {"surprise": 1})
		self.assertEqual(len(errors), 1)


class TestRedaction(unittest.TestCase):
	def test_secret_keys_are_redacted_and_others_kept(self):
		params = {
			"doctype": "Sales Order",
			"password": "hunter2",
			"api_key": "sk-live-123",
			"access_token": "abc",
			"client_secret": "shh",
			"customer": "Acme Inc",
			"limit": 20,
		}
		out = audit.redact_params(params)

		self.assertEqual(out["password"], audit.REDACTED)
		self.assertEqual(out["api_key"], audit.REDACTED)
		self.assertEqual(out["access_token"], audit.REDACTED)
		self.assertEqual(out["client_secret"], audit.REDACTED)
		self.assertEqual(out["doctype"], "Sales Order")
		self.assertEqual(out["customer"], "Acme Inc")
		self.assertEqual(out["limit"], 20)
		self.assertEqual(set(out), set(params), "redaction keeps the shape; only values change")

	def test_redaction_is_recursive_and_case_insensitive(self):
		params = {"data": {"items": [{"API_KEY": "x", "item_code": "ITEM-1"}], "Password": "p"}}
		out = audit.redact_params(params)
		self.assertEqual(out["data"]["items"][0]["API_KEY"], audit.REDACTED)
		self.assertEqual(out["data"]["items"][0]["item_code"], "ITEM-1")
		self.assertEqual(out["data"]["Password"], audit.REDACTED)

	def test_non_dict_values_pass_through(self):
		self.assertEqual(audit.redact_params("plain"), "plain")
		self.assertEqual(audit.redact_params([1, 2, 3]), [1, 2, 3])
		self.assertIsNone(audit.redact_params(None))

	def test_is_secret_key_matches_across_separators_and_case(self):
		for key in (
			"password",
			"API-KEY",
			"apiKey",
			"x_auth_token",
			"client_secret",
			"private_key",
			"Authorization",
		):
			self.assertTrue(audit.is_secret_key(key), key)

	def test_is_secret_key_leaves_ordinary_keys_alone(self):
		for key in ("doctype", "customer", "item_code", "keyword", "grand_total", 42, None):
			self.assertFalse(audit.is_secret_key(key), key)

	def test_is_secret_key_over_matches_on_purpose(self):
		"""Blunt substring matching redacts a few innocent fields. That is the
		side we choose to err on."""
		self.assertTrue(audit.is_secret_key("tokenizer_notes"))

	def test_serialize_params_redacts_and_truncates(self):
		text = audit.serialize_params({"api_key": "secret", "note": "x" * (audit.MAX_PARAMS_CHARS + 500)})
		self.assertNotIn("secret", text)
		self.assertIn(audit.REDACTED, text)
		self.assertIn("truncated", text)
		self.assertLessEqual(len(text), audit.MAX_PARAMS_CHARS + 100)

	def test_serialize_params_accepts_a_json_string(self):
		text = audit.serialize_params('{"token": "abc", "doctype": "Item"}')
		self.assertNotIn("abc", text)
		self.assertIn("Item", text)

	def test_serialize_params_survives_unserializable_values(self):
		text = audit.serialize_params({"when": object()})
		self.assertIsInstance(text, str)
		self.assertTrue(text)


if __name__ == "__main__":
	unittest.main(verbosity=2)
