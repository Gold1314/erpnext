# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = ["Company", "Customer", "Item"]
IGNORE_TEST_RECORD_DEPENDENCIES = []

COMPANY = "_Test Company"
CUSTOMER = "_Test Customer"


def make_contract(**overrides):
	doc = frappe.get_doc(
		{
			"doctype": "Revenue Contract",
			"company": COMPANY,
			"customer": CUSTOMER,
			"contract_date": "2026-01-01",
			"deferred_revenue_account": frappe.db.get_value(
				"Account", {"account_name": "Deferred Revenue", "company": COMPANY}
			),
			"obligations": [
				{
					"description": "Software licence",
					"stated_amount": 700,
					"ssp": 800,
					"satisfaction_method": "Point in Time",
					"income_account": frappe.db.get_value(
						"Account", {"account_name": "Sales", "company": COMPANY}
					),
				},
				{
					"description": "Support (H1 2026)",
					"stated_amount": 300,
					"ssp": 200,
					"satisfaction_method": "Over Time",
					"service_start_date": "2026-01-01",
					"service_end_date": "2026-06-30",
					"income_account": frappe.db.get_value(
						"Account", {"account_name": "Service", "company": COMPANY}
					),
				},
			],
		}
	)
	doc.update(overrides)
	return doc.insert()


class IntegrationTestRevenueContract(IntegrationTestCase):
	"""Integration tests for Revenue Contract (allocation, plan, posting)."""

	def test_allocation_and_plan_on_validate(self):
		contract = make_contract()
		self.assertEqual(flt(contract.transaction_price), 1000)

		# 800:200 relative-SSP split of the 1000 transaction price
		self.assertEqual(flt(contract.obligations[0].allocated_amount), 800)
		self.assertEqual(flt(contract.obligations[1].allocated_amount), 200)
		self.assertEqual(flt(contract.obligations[0].allocation_pct), 80)

		# 1 point-in-time row (undated) + 6 monthly over-time rows
		self.assertEqual(len(contract.recognition_plan), 7)
		plan_total = flt(sum(flt(row.amount) for row in contract.recognition_plan), 2)
		self.assertEqual(plan_total, flt(contract.transaction_price, 2))

		pit_rows = [r for r in contract.recognition_plan if r.obligation_idx == 1]
		self.assertEqual(len(pit_rows), 1)
		self.assertIsNone(pit_rows[0].period_end)

	def test_submit_activates_and_posts_recognition(self):
		contract = make_contract()
		contract.submit()
		self.assertEqual(contract.status, "Active")

		# over-time rows through March are due; point-in-time row is not
		result = contract.post_recognition(until_date="2026-03-31")
		self.assertEqual(result["posted"], 3)
		self.assertEqual(result["pending_event"], 1)

		contract.reload()
		self.assertEqual(flt(contract.obligations[1].recognized_amount, 2), 100)

		posted_rows = [r for r in contract.recognition_plan if r.posted]
		self.assertEqual(len(posted_rows), 3)
		for row in posted_rows:
			self.assertTrue(row.journal_entry)
			self.assertEqual(frappe.db.get_value("Journal Entry", row.journal_entry, "docstatus"), 1)

		# satisfy the point-in-time obligation, then everything through June posts
		contract.mark_obligation_satisfied(1, "2026-04-15")
		contract.reload()
		result = contract.post_recognition(until_date="2026-06-30")
		self.assertEqual(result["posted"], 4)  # licence + Apr, May, Jun support

		contract.reload()
		self.assertEqual(contract.status, "Completed")
		self.assertEqual(flt(contract.obligations[0].recognized_amount, 2), 800)
		self.assertEqual(flt(contract.obligations[1].recognized_amount, 2), 200)

	def test_cancel_blocked_after_posting(self):
		contract = make_contract()
		contract.submit()
		contract.post_recognition(until_date="2026-01-31")
		contract.reload()
		self.assertRaises(frappe.ValidationError, contract.cancel)

	def test_cancel_allowed_when_nothing_posted(self):
		contract = make_contract()
		contract.submit()
		contract.reload()
		contract.cancel()
		self.assertEqual(contract.status, "Cancelled")
