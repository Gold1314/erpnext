# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_last_day, get_link_to_form, getdate, nowdate

from erpnext.assets.doctype.asset_category.asset_category import get_asset_category_account
from erpnext.assets.leasing.engine import (
	build_schedule,
	classify_short_term,
	generate_payment_rows,
	term_months,
)
from erpnext.assets.leasing.models import LeasePayment, LeaseTerms


class LeaseContract(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.assets.doctype.lease_amortization_entry.lease_amortization_entry import (
			LeaseAmortizationEntry,
		)
		from erpnext.assets.doctype.lease_contract_payment.lease_contract_payment import (
			LeaseContractPayment,
		)

		amended_from: DF.Link | None
		auto_post_monthly: DF.Check
		amortization_schedule: DF.Table[LeaseAmortizationEntry]
		annual_discount_rate: DF.Percent
		asset_category: DF.Link | None
		asset_description: DF.SmallText | None
		asset_item: DF.Link | None
		commencement_date: DF.Date
		commencement_journal_entry: DF.Link | None
		company: DF.Link
		cost_center: DF.Link | None
		end_date: DF.Date
		initial_liability: DF.Currency
		initial_rou: DF.Currency
		interest_expense_account: DF.Link | None
		is_short_term: DF.Check
		lease_liability_account: DF.Link | None
		lease_name: DF.Data | None
		lessor: DF.Link | None
		location: DF.Link | None
		payment_account: DF.Link
		payment_amount: DF.Currency
		payment_frequency: DF.Literal["Monthly", "Quarterly", "Annually"]
		payment_timing: DF.Literal["End of Period", "Beginning of Period"]
		payments: DF.Table[LeaseContractPayment]
		rou_asset: DF.Link | None
		short_term_expense_account: DF.Link | None
		total_interest: DF.Currency
	# end: auto-generated types

	def validate(self):
		self.validate_dates()
		self.validate_payments()

		terms = self.get_lease_terms()
		self.is_short_term = 1 if classify_short_term(terms) else 0
		self.validate_required_fields()

		if self.docstatus == 0:
			schedule = self.run_engine(terms)
			self.set_computed_values(schedule)
			self.set_amortization_schedule(schedule)

	def validate_dates(self):
		if getdate(self.end_date) <= getdate(self.commencement_date):
			frappe.throw(_("End Date must be after Commencement Date."))

	def validate_payments(self):
		if not self.payments:
			frappe.throw(_("Add at least one lease payment (or use Generate Payments)."))

		commencement, end = getdate(self.commencement_date), getdate(self.end_date)
		for row in self.payments:
			if flt(row.amount) <= 0:
				frappe.throw(_("Row #{0}: Payment amount must be greater than zero.").format(row.idx))
			due = getdate(row.due_date)
			if due < commencement or due > end:
				frappe.throw(
					_("Row #{0}: Payment due date {1} falls outside the lease term.").format(
						row.idx, frappe.format(due, {"fieldtype": "Date"})
					)
				)

	def validate_required_fields(self):
		if self.is_short_term:
			if not self.short_term_expense_account:
				frappe.throw(
					_("Short Term Lease Expense Account is required for short-term leases."),
					frappe.MandatoryError,
				)
			return

		missing = [
			label
			for fieldname, label in (
				("asset_category", _("Asset Category")),
				("asset_item", _("Asset Item")),
				("location", _("Location")),
				("lease_liability_account", _("Lease Liability Account")),
				("interest_expense_account", _("Interest Expense Account")),
			)
			if not self.get(fieldname)
		]
		if missing:
			frappe.throw(
				_("The following fields are required for capitalized leases: {0}").format(
					", ".join(missing)
				),
				frappe.MandatoryError,
			)

	def get_lease_terms(self) -> LeaseTerms:
		return LeaseTerms(
			commencement_date=getdate(self.commencement_date),
			end_date=getdate(self.end_date),
			payments=[
				LeasePayment(due_date=getdate(row.due_date), amount=flt(row.amount))
				for row in self.payments
			],
			annual_discount_rate_pct=flt(self.annual_discount_rate),
			payment_timing=self.payment_timing or "End of Period",
		)

	def run_engine(self, terms: LeaseTerms):
		try:
			return build_schedule(terms)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid Lease Terms"))

	def set_computed_values(self, schedule):
		if self.is_short_term:
			# short-term exemption: no liability, no ROU - payments are expensed
			self.initial_liability = 0
			self.initial_rou = 0
			self.total_interest = 0
		else:
			self.initial_liability = schedule.initial_liability
			self.initial_rou = schedule.initial_rou
			self.total_interest = schedule.total_interest

	def set_amortization_schedule(self, schedule):
		self.set("amortization_schedule", [])
		for row in schedule.rows:
			values = {
				"period_start": row.period_start,
				"period_end": row.period_end,
				"payment": row.payment,
				"posted": 0,
			}
			if not self.is_short_term:
				values.update(
					{
						"opening_liability": row.opening_liability,
						"interest": row.interest,
						"principal": row.principal,
						"closing_liability": row.closing_liability,
						"opening_rou": row.opening_rou,
						"rou_depreciation": row.rou_depreciation,
						"closing_rou": row.closing_rou,
					}
				)
			self.append("amortization_schedule", values)

	@frappe.whitelist()
	def generate_payments(self):
		"""Pre-fill the payments child table from frequency, amount and term.

		Convenience only - the child table remains the source of truth and
		can be edited freely afterwards. Draft documents only.
		"""
		if self.docstatus != 0:
			frappe.throw(_("Payments can only be generated on a draft Lease Contract."))
		if not flt(self.payment_amount) or not self.payment_frequency:
			frappe.throw(_("Set Payment Frequency and Payment Amount first."))

		try:
			rows = generate_payment_rows(
				getdate(self.commencement_date),
				getdate(self.end_date),
				self.payment_frequency,
				flt(self.payment_amount),
				self.payment_timing or "End of Period",
			)
		except ValueError as e:
			frappe.throw(str(e), title=_("Cannot Generate Payments"))

		self.set("payments", [])
		for payment in rows:
			self.append("payments", {"due_date": payment.due_date, "amount": payment.amount})
		self.save()
		return len(rows)

	def on_submit(self):
		if self.is_short_term:
			# nothing is capitalized; post_monthly_entries expenses each payment
			return

		asset = self.create_rou_asset()
		self.db_set("rou_asset", asset.name)

		journal_entry = self.post_commencement_journal_entry()
		self.db_set("commencement_journal_entry", journal_entry.name)

		frappe.msgprint(
			_("Right-of-Use Asset {0} created as a draft. Review its depreciation schedule and submit it.").format(
				get_link_to_form("Asset", asset.name)
			)
		)

	def create_rou_asset(self):
		"""Create the ROU asset as a draft, standard `Asset`.

		- ``asset_type = "Existing Asset"`` so no purchase document is
		  required and the Asset itself posts no GL on submit (capitalization
		  is booked by the commencement Journal Entry instead).
		- Straight-line depreciation over the lease term in months, monthly
		  frequency, zero salvage - the assets module then owns depreciation
		  posting entirely (this module never posts depreciation).
		- Left in draft deliberately: the Asset controller generates the
		  depreciation schedule on save and asks the user to review and
		  submit ("Please check, edit if needed, and submit the Asset.").
		"""
		months = term_months(getdate(self.commencement_date), getdate(self.end_date))

		asset = frappe.get_doc(
			{
				"doctype": "Asset",
				"asset_type": "Existing Asset",
				"asset_name": self.lease_name or _("Right-of-Use: {0}").format(self.name),
				"item_code": self.asset_item,
				"asset_category": self.asset_category,
				"company": self.company,
				"location": self.location,
				"cost_center": self.cost_center,
				"asset_owner": "Company",
				"asset_owner_company": self.company,
				"asset_quantity": 1,
				"purchase_date": self.commencement_date,
				"available_for_use_date": self.commencement_date,
				"net_purchase_amount": self.initial_rou,
				"calculate_depreciation": 1,
				"finance_books": [
					{
						"depreciation_method": "Straight Line",
						"total_number_of_depreciations": months,
						"frequency_of_depreciation": 1,
						"depreciation_start_date": get_last_day(self.commencement_date),
						"expected_value_after_useful_life": 0,
					}
				],
			}
		)
		asset.flags.ignore_permissions = True
		asset.insert()
		return asset

	def post_commencement_journal_entry(self):
		"""Dr fixed asset account (from the Asset Category), Cr lease liability.

		The ROU asset is created as an "Existing Asset" which posts no GL on
		submit, so this Journal Entry is what puts both the ROU asset value
		and the lease liability on the books at commencement.
		"""
		fixed_asset_account = get_asset_category_account(
			"fixed_asset_account", asset_category=self.asset_category, company=self.company
		)
		if not fixed_asset_account:
			frappe.throw(
				_("Set Fixed Asset Account in Asset Category {0} for company {1}.").format(
					self.asset_category, self.company
				)
			)

		remark = _("Lease commencement for {0} ({1})").format(self.name, self.lease_name or self.lessor or "")
		return self.make_journal_entry(
			posting_date=self.commencement_date,
			lines=[
				(fixed_asset_account, flt(self.initial_rou)),
				(self.lease_liability_account, -flt(self.initial_liability)),
			],
			remark=remark,
		)

	def make_journal_entry(self, posting_date, lines, remark):
		"""Build and submit a Journal Entry from (account, signed_amount) pairs.

		Positive amounts are debits, negative amounts credits. The Journal
		Entry cannot reference the Lease Contract on its rows (the child
		``reference_type`` Select does not include Lease Contract), so the
		link is kept on this document instead and the remark carries context.
		"""
		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Journal Entry"
		journal_entry.company = self.company
		journal_entry.posting_date = posting_date
		journal_entry.user_remark = remark

		for account, amount in lines:
			amount = flt(amount, 2)
			if not amount:
				continue
			side = "debit" if amount > 0 else "credit"
			journal_entry.append(
				"accounts",
				{
					"account": account,
					side: abs(amount),
					f"{side}_in_account_currency": abs(amount),
					"cost_center": self.cost_center,
					"user_remark": remark,
				},
			)

		journal_entry.flags.ignore_permissions = True
		journal_entry.save()
		journal_entry.submit()
		return journal_entry

	@frappe.whitelist()
	def post_monthly_entries(self, until_date=None):
		"""Post one Journal Entry per unposted schedule period ended on or
		before ``until_date`` (default: today).

		Capitalized lease: Dr interest expense (accretion), Dr lease
		liability (principal), Cr payment account. Depreciation of the ROU
		asset is NOT posted here - the Asset's own depreciation schedule
		handles it (see DESIGN.md).

		Short-term lease: Dr short-term lease expense, Cr payment account,
		per payment.
		"""
		if self.docstatus != 1:
			frappe.throw(_("Lease Contract must be submitted before posting entries."))

		until = getdate(until_date or nowdate())
		posted = 0

		for row in self.amortization_schedule:
			if row.posted or getdate(row.period_end) > until:
				continue

			if self.is_short_term:
				lines = [
					(self.short_term_expense_account, flt(row.payment)),
					(self.payment_account, -flt(row.payment)),
				]
			else:
				lines = [
					(self.interest_expense_account, flt(row.interest)),
					(self.lease_liability_account, flt(row.principal)),
					(self.payment_account, -flt(row.payment)),
				]

			journal_entry_name = None
			if any(flt(amount, 2) for _account, amount in lines):
				remark = _("Lease entry for {0}, period ending {1}").format(
					self.name, frappe.format(getdate(row.period_end), {"fieldtype": "Date"})
				)
				journal_entry = self.make_journal_entry(
					posting_date=row.period_end, lines=lines, remark=remark
				)
				journal_entry_name = journal_entry.name

			row.db_set("journal_entry", journal_entry_name)
			row.db_set("posted", 1)
			posted += 1

		if posted:
			frappe.msgprint(_("Posted {0} lease period(s).").format(posted))
		else:
			frappe.msgprint(_("No unposted lease periods ending on or before {0}.").format(until))

		return posted

	def on_cancel(self):
		posted_rows = [row for row in self.amortization_schedule if row.posted]
		if posted_rows:
			frappe.throw(
				_(
					"Cannot cancel: {0} schedule period(s) have posted Journal Entries. Cancel those Journal Entries first."
				).format(len(posted_rows))
			)

		if self.rou_asset:
			asset_docstatus = frappe.db.get_value("Asset", self.rou_asset, "docstatus")
			if asset_docstatus == 1:
				frappe.throw(
					_("Cancel the Right-of-Use Asset {0} before cancelling this Lease Contract.").format(
						get_link_to_form("Asset", self.rou_asset)
					)
				)
			elif asset_docstatus == 0:
				rou_asset = self.rou_asset
				self.db_set("rou_asset", None)
				for schedule in frappe.get_all(
					"Asset Depreciation Schedule", filters={"asset": rou_asset, "docstatus": 0}, pluck="name"
				):
					frappe.delete_doc("Asset Depreciation Schedule", schedule, force=1)
				frappe.delete_doc("Asset", rou_asset, force=1)

		if self.commencement_journal_entry:
			if frappe.db.get_value("Journal Entry", self.commencement_journal_entry, "docstatus") == 1:
				frappe.get_doc("Journal Entry", self.commencement_journal_entry).cancel()


def post_scheduled_lease_entries():
	"""Monthly scheduler entry point.

	Posts due interest/principal (or short-term expense) entries for submitted
	Lease Contracts that opted in via ``auto_post_monthly``. Opt-in is per
	contract and off by default: posting journal entries on a timer is a
	deliberate choice, not something a lease should start doing on upgrade.
	Errors on one contract never stop the rest.
	"""
	contracts = frappe.get_all(
		"Lease Contract",
		filters={"docstatus": 1, "auto_post_monthly": 1},
		pluck="name",
	)

	for name in contracts:
		try:
			frappe.db.savepoint("scheduled_lease_posting")
			frappe.get_doc("Lease Contract", name).post_monthly_entries()
			# Commit per contract so one failure cannot undo earlier postings.
			# Skipped in tests, where the test case owns the transaction.
			if not frappe.in_test:
				frappe.db.commit()
		except Exception:
			# Roll back to the savepoint rather than the whole transaction: this
			# restores a usable connection after a database error (on Postgres an
			# aborted transaction would fail every later contract, and log_error
			# itself) without discarding an enclosing test transaction.
			frappe.db.rollback(save_point="scheduled_lease_posting")
			frappe.log_error(
				title=f"Scheduled lease posting failed for {name}",
				message=frappe.get_traceback(with_context=True),
			)
