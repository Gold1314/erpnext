# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from erpnext.accounts.doctype.sod_rule.sod_rule import create_default_sod_rules, run_sod_scan

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []

TEST_USER = "test_sod_conflicted_user@example.com"


class IntegrationTestSoDRule(IntegrationTestCase):
	"""
	Integration tests for SoDRule.
	Use this class for testing interactions between multiple components.
	"""

	def setUp(self):
		super().setUp()
		self.create_test_rule()
		self.create_test_user()

	def create_test_rule(self):
		if not frappe.db.exists("SoD Rule", "_Test SoD Rule"):
			frappe.get_doc(
				{
					"doctype": "SoD Rule",
					"rule_name": "_Test SoD Rule",
					"description": "Users must not both maintain suppliers and post payments.",
					"risk_level": "High",
					"enabled": 1,
					"capabilities": [
						{"side": "First Function", "role": "Purchase Master Manager"},
						{"side": "Second Function", "role": "Accounts User"},
					],
				}
			).insert()

	def create_test_user(self):
		if not frappe.db.exists("User", TEST_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": TEST_USER,
					"first_name": "SoD",
					"last_name": "Conflicted",
					"user_type": "System User",
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

		user = frappe.get_doc("User", TEST_USER)
		user.add_roles("Purchase Master Manager", "Accounts User")

	def test_rule_requires_both_sides(self):
		rule = frappe.get_doc(
			{
				"doctype": "SoD Rule",
				"rule_name": "_Test One Sided Rule",
				"risk_level": "Low",
				"enabled": 1,
				"capabilities": [{"side": "First Function", "role": "Accounts User"}],
			}
		)
		self.assertRaises(frappe.ValidationError, rule.insert)

	def test_scan_detects_and_resolves_violation(self):
		result = run_sod_scan()
		self.assertGreaterEqual(result["new_violations"] + result["refreshed_violations"], 1)

		log_name = frappe.db.get_value(
			"SoD Violation Log",
			{"user": TEST_USER, "sod_rule": "_Test SoD Rule", "status": "Open"},
			"name",
		)
		self.assertIsNotNone(log_name)

		log = frappe.get_doc("SoD Violation Log", log_name)
		self.assertEqual(log.first_function_roles, "Purchase Master Manager")
		self.assertEqual(log.second_function_roles, "Accounts User")
		self.assertEqual(log.risk_level, "High")

		# A second scan must refresh the same Open log, not duplicate it
		run_sod_scan()
		open_logs = frappe.get_all(
			"SoD Violation Log",
			filters={"user": TEST_USER, "sod_rule": "_Test SoD Rule", "status": "Open"},
		)
		self.assertEqual(len(open_logs), 1)

		# Removing one conflicting role auto-resolves the violation
		user = frappe.get_doc("User", TEST_USER)
		user.remove_roles("Accounts User")
		run_sod_scan()

		log.reload()
		self.assertEqual(log.status, "Resolved")
		self.assertIsNotNone(log.resolved_on)

	def test_default_rule_pack_is_idempotent(self):
		first = create_default_sod_rules()
		second = create_default_sod_rules()

		self.assertEqual(second["created"], [])
		self.assertEqual(
			len(second["skipped"]), len(first["created"]) + len(first["skipped"]),
		)
