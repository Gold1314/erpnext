# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, get_last_day, getdate

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = ["Company", "Supplier", "Location", "Asset Category", "Item"]
IGNORE_TEST_RECORD_DEPENDENCIES = []

COMPANY = "_Test Company"


def get_or_create_account(account_name, parent_account, root_type, account_type=None):
	name = frappe.db.get_value("Account", {"account_name": account_name, "company": COMPANY})
	if name:
		return name

	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent_account,
			"root_type": root_type,
			"account_type": account_type,
			"company": COMPANY,
		}
	)
	account.insert(ignore_permissions=True)
	return account.name


def create_lease_accounts():
	return {
		"lease_liability_account": get_or_create_account(
			"_Test Lease Liability", "Duties and Taxes - _TC", "Liability"
		),
		"interest_expense_account": get_or_create_account(
			"_Test Lease Interest Expense", "Indirect Expenses - _TC", "Expense"
		),
		"short_term_expense_account": get_or_create_account(
			"_Test Short Term Lease Expense", "Indirect Expenses - _TC", "Expense"
		),
		"payment_account": "Creditors - _TC",
	}


def create_rou_asset_dependencies():
	if not frappe.db.exists("Asset Category", "_Test Lease ROU Category"):
		frappe.get_doc(
			{
				"doctype": "Asset Category",
				"asset_category_name": "_Test Lease ROU Category",
				"accounts": [
					{
						"company_name": COMPANY,
						"fixed_asset_account": get_or_create_account(
							"_Test Right of Use Assets", "Fixed Assets - _TC", "Asset", "Fixed Asset"
						),
						"accumulated_depreciation_account": get_or_create_account(
							"_Test Accumulated ROU Depreciation",
							"Fixed Assets - _TC",
							"Asset",
							"Accumulated Depreciation",
						),
						"depreciation_expense_account": get_or_create_account(
							"_Test ROU Depreciation Expense", "Indirect Expenses - _TC", "Expense", "Depreciation"
						),
					}
				],
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item", "_Test Lease ROU Item"):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_Test Lease ROU Item",
				"item_name": "_Test Lease ROU Item",
				"item_group": "All Item Groups",
				"is_stock_item": 0,
				"is_fixed_asset": 1,
				"asset_category": "_Test Lease ROU Category",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Location", "_Test Lease Location"):
		frappe.get_doc({"doctype": "Location", "location_name": "_Test Lease Location"}).insert(
			ignore_permissions=True
		)


def make_lease_contract(months=36, amount=1000.0, rate=6.0, submit=False, **overrides):
	accounts = create_lease_accounts()
	create_rou_asset_dependencies()

	commencement = getdate("2026-01-01")
	end = add_days(add_months(commencement, months), -1)

	contract = frappe.get_doc(
		{
			"doctype": "Lease Contract",
			"company": COMPANY,
			"lessor": "_Test Supplier",
			"lease_name": f"_Test Lease {months}m",
			"commencement_date": commencement,
			"end_date": end,
			"annual_discount_rate": rate,
			"payment_timing": "End of Period",
			"payment_frequency": "Monthly",
			"payment_amount": amount,
			"asset_category": "_Test Lease ROU Category",
			"asset_item": "_Test Lease ROU Item",
			"location": "_Test Lease Location",
			"cost_center": "_Test Cost Center - _TC",
			"payments": [
				{"due_date": get_last_day(add_months(commencement, tick - 1)), "amount": amount}
				for tick in range(1, months + 1)
			],
			**accounts,
			**overrides,
		}
	)
	contract.insert()
	if submit:
		contract.submit()
	return contract


class IntegrationTestLeaseContract(IntegrationTestCase):
	"""
	Integration tests for LeaseContract.
	Use this class for testing interactions between multiple components.
	"""

	def test_validate_computes_schedule_and_classification(self):
		contract = make_lease_contract(months=36)

		self.assertEqual(contract.is_short_term, 0)
		self.assertGreater(contract.initial_liability, 0)
		self.assertEqual(contract.initial_liability, contract.initial_rou)
		self.assertEqual(len(contract.amortization_schedule), 36)
		# schedule closes both liability and ROU to exactly zero
		last = contract.amortization_schedule[-1]
		self.assertEqual(last.closing_liability, 0)
		self.assertEqual(last.closing_rou, 0)

	def test_short_term_lease_classification(self):
		contract = make_lease_contract(months=12)

		self.assertEqual(contract.is_short_term, 1)
		self.assertEqual(contract.initial_liability, 0)
		self.assertEqual(contract.initial_rou, 0)
		self.assertEqual(len(contract.amortization_schedule), 12)

	def test_date_order_validation(self):
		self.assertRaises(
			frappe.ValidationError,
			make_lease_contract,
			months=12,
			end_date=getdate("2025-01-01"),
		)

	def test_generate_payments_fills_child_table(self):
		contract = make_lease_contract(months=24, amount=500.0)
		contract.payment_frequency = "Quarterly"
		contract.generate_payments()
		contract.reload()

		self.assertEqual(len(contract.payments), 8)
		self.assertEqual(contract.payments[0].amount, 500.0)

	def test_submit_creates_draft_rou_asset_and_commencement_je(self):
		contract = make_lease_contract(months=36, submit=True)

		self.assertTrue(contract.rou_asset)
		asset = frappe.get_doc("Asset", contract.rou_asset)
		self.assertEqual(asset.docstatus, 0)
		self.assertEqual(asset.asset_type, "Existing Asset")
		self.assertEqual(asset.net_purchase_amount, contract.initial_rou)
		self.assertEqual(asset.finance_books[0].total_number_of_depreciations, 36)
		self.assertEqual(asset.finance_books[0].frequency_of_depreciation, 1)

		self.assertTrue(contract.commencement_journal_entry)
		journal_entry = frappe.get_doc("Journal Entry", contract.commencement_journal_entry)
		self.assertEqual(journal_entry.docstatus, 1)
		self.assertEqual(journal_entry.total_debit, contract.initial_rou)

	def test_post_monthly_entries_posts_interest_and_principal(self):
		contract = make_lease_contract(months=36, submit=True)
		first_period_end = contract.amortization_schedule[0].period_end

		posted = contract.post_monthly_entries(until_date=first_period_end)
		contract.reload()

		self.assertEqual(posted, 1)
		row = contract.amortization_schedule[0]
		self.assertEqual(row.posted, 1)
		self.assertTrue(row.journal_entry)

		journal_entry = frappe.get_doc("Journal Entry", row.journal_entry)
		self.assertEqual(journal_entry.total_debit, row.payment)
		accounts = {line.account: line for line in journal_entry.accounts}
		self.assertEqual(accounts[contract.interest_expense_account].debit, row.interest)
		self.assertEqual(accounts[contract.lease_liability_account].debit, row.principal)
		self.assertEqual(accounts[contract.payment_account].credit, row.payment)

	def test_short_term_posting_expenses_payments(self):
		contract = make_lease_contract(months=12, submit=True)
		self.assertFalse(contract.rou_asset)
		self.assertFalse(contract.commencement_journal_entry)

		first_period_end = contract.amortization_schedule[0].period_end
		contract.post_monthly_entries(until_date=first_period_end)
		contract.reload()

		row = contract.amortization_schedule[0]
		self.assertTrue(row.journal_entry)
		journal_entry = frappe.get_doc("Journal Entry", row.journal_entry)
		accounts = {line.account: line for line in journal_entry.accounts}
		self.assertEqual(accounts[contract.short_term_expense_account].debit, row.payment)

	def test_cancel_blocked_after_posting(self):
		contract = make_lease_contract(months=36, submit=True)
		contract.post_monthly_entries(until_date=contract.amortization_schedule[0].period_end)
		contract.reload()

		self.assertRaises(frappe.ValidationError, contract.cancel)

	def test_cancel_deletes_draft_asset_and_cancels_commencement_je(self):
		contract = make_lease_contract(months=36, submit=True)
		rou_asset = contract.rou_asset
		commencement_je = contract.commencement_journal_entry

		contract.cancel()

		self.assertFalse(frappe.db.exists("Asset", rou_asset))
		self.assertEqual(frappe.db.get_value("Journal Entry", commencement_je, "docstatus"), 2)
