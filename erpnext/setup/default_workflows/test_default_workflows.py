# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Schema-sanity tests for the shipped workflow definitions.

``definitions.py`` is pure data (no frappe import), so every test in this
module runs without a site:

    python -m unittest erpnext.setup.default_workflows.test_default_workflows
"""

import unittest

from erpnext.setup.default_workflows.definitions import (
	DEFAULT_APPROVAL_THRESHOLD,
	THRESHOLD_PLACEHOLDER,
	WORKFLOW_ACTIONS,
	WORKFLOW_DEFINITIONS,
	WORKFLOW_STATES,
)

# Roles verified against permission blocks shipped in erpnext doctype JSONs.
KNOWN_ROLES = {
	"Purchase User",
	"Purchase Manager",
	"Accounts User",
	"Accounts Manager",
	"Sales User",
	"Sales Manager",
	"Stock User",
	"Stock Manager",
}

EXPECTED_DOCUMENT_TYPES = {
	"purchase-order-approval": "Purchase Order",
	"journal-entry-approval": "Journal Entry",
	"sales-invoice-discount-approval": "Sales Invoice",
	"material-request-approval": "Material Request",
}

REQUIRED_WORKFLOW_KEYS = {
	"workflow_name",
	"document_type",
	"workflow_state_field",
	"is_active",
	"send_email_alert",
	"states",
	"transitions",
}


class TestDefaultWorkflowDefinitions(unittest.TestCase):
	def test_all_expected_slugs_present(self):
		self.assertEqual(set(WORKFLOW_DEFINITIONS), set(EXPECTED_DOCUMENT_TYPES))

	def test_document_types(self):
		for slug, doctype in EXPECTED_DOCUMENT_TYPES.items():
			self.assertEqual(WORKFLOW_DEFINITIONS[slug]["document_type"], doctype)

	def test_required_keys(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			missing = REQUIRED_WORKFLOW_KEYS - set(definition)
			self.assertFalse(missing, f"{slug}: missing keys {missing}")
			self.assertTrue(definition["workflow_name"], f"{slug}: empty workflow_name")
			self.assertTrue(definition.get("description"), f"{slug}: empty description")

	def test_workflow_state_field(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			self.assertEqual(
				definition["workflow_state_field"],
				"workflow_state",
				f"{slug}: workflow_state_field must be 'workflow_state'",
			)

	def test_state_rows(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			self.assertTrue(definition["states"], f"{slug}: no states")
			seen = set()
			for row in definition["states"]:
				self.assertTrue(row.get("state"), f"{slug}: state row without a state name")
				self.assertNotIn(row["state"], seen, f"{slug}: duplicate state {row['state']}")
				seen.add(row["state"])
				self.assertIn(
					row.get("doc_status"),
					{"0", "1", "2"},
					f"{slug}/{row['state']}: doc_status must be a string '0'/'1'/'2'",
				)
				self.assertIn(
					row.get("allow_edit"),
					KNOWN_ROLES,
					f"{slug}/{row['state']}: allow_edit must be a known role",
				)

	def test_every_workflow_has_a_submitted_state(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			doc_statuses = {row["doc_status"] for row in definition["states"]}
			self.assertIn("1", doc_statuses, f"{slug}: no submitted (doc_status '1') state")
			self.assertIn("0", doc_statuses, f"{slug}: no draft (doc_status '0') state")

	def test_transitions_reference_defined_states(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			states = {row["state"] for row in definition["states"]}
			self.assertTrue(definition["transitions"], f"{slug}: no transitions")
			for row in definition["transitions"]:
				self.assertIn(row.get("state"), states, f"{slug}: transition from undefined state")
				self.assertIn(
					row.get("next_state"), states, f"{slug}: transition to undefined state"
				)
				self.assertTrue(row.get("action"), f"{slug}: transition without an action")
				self.assertIn(
					row.get("allowed"),
					KNOWN_ROLES,
					f"{slug}: transition allowed must be a known role",
				)
				self.assertIn(row.get("allow_self_approval", 0), (0, 1))

	def test_transition_actions_have_masters(self):
		shipped = {row["workflow_action_name"] for row in WORKFLOW_ACTIONS}
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			for row in definition["transitions"]:
				self.assertIn(
					row["action"], shipped, f"{slug}: action {row['action']} has no shipped master"
				)

	def test_states_have_masters(self):
		shipped = {row["workflow_state_name"] for row in WORKFLOW_STATES}
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			for row in definition["states"]:
				self.assertIn(
					row["state"], shipped, f"{slug}: state {row['state']} has no shipped master"
				)

	def test_conditions_are_valid_python_expressions(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			for row in definition["transitions"]:
				condition = row.get("condition")
				if not condition:
					continue
				resolved = condition.replace(
					THRESHOLD_PLACEHOLDER, str(float(DEFAULT_APPROVAL_THRESHOLD))
				)
				try:
					compile(resolved, "<workflow condition>", "eval")
				except SyntaxError as e:
					self.fail(f"{slug}: condition {resolved!r} is not a valid expression: {e}")

	def test_threshold_placeholder_only_in_purchase_order_workflow(self):
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			uses_placeholder = any(
				THRESHOLD_PLACEHOLDER in (row.get("condition") or "")
				for row in definition["transitions"]
			)
			if slug == "purchase-order-approval":
				self.assertTrue(uses_placeholder, f"{slug}: threshold placeholder missing")
			else:
				self.assertFalse(uses_placeholder, f"{slug}: unexpected threshold placeholder")

	def test_default_threshold_is_positive_number(self):
		self.assertIsInstance(DEFAULT_APPROVAL_THRESHOLD, (int, float))
		self.assertGreater(DEFAULT_APPROVAL_THRESHOLD, 0)

	def test_workflow_names_unique(self):
		names = [d["workflow_name"] for d in WORKFLOW_DEFINITIONS.values()]
		self.assertEqual(len(names), len(set(names)))

	def test_pending_states_route_to_approved_and_rejected(self):
		"""Every workflow with a Pending Approval state must offer both an
		Approve and a Reject transition out of it, and Rejected must route
		back to Draft."""
		for slug, definition in WORKFLOW_DEFINITIONS.items():
			states = {row["state"] for row in definition["states"]}
			if "Pending Approval" not in states:
				continue
			outgoing = {
				(row["action"], row["next_state"])
				for row in definition["transitions"]
				if row["state"] == "Pending Approval"
			}
			self.assertIn(("Approve", "Approved"), outgoing, f"{slug}: no Approve path")
			self.assertIn(("Reject", "Rejected"), outgoing, f"{slug}: no Reject path")
			back = {
				(row["action"], row["next_state"])
				for row in definition["transitions"]
				if row["state"] == "Rejected"
			}
			self.assertIn(("Review", "Draft"), back, f"{slug}: Rejected does not route back to Draft")


if __name__ == "__main__":
	unittest.main()
