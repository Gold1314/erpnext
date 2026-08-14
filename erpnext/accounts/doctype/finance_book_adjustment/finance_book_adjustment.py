# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, getdate

from erpnext.accounts.multibook import engine, loaders
from erpnext.accounts.multibook.models import MANUAL_AMOUNT
from erpnext.accounts.utils import get_account_currency

# editing any of these on a draft invalidates previously computed lines
COMPUTE_INPUT_FIELDS = ("company", "finance_book", "from_date", "to_date")


class FinanceBookAdjustment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.finance_book_adjustment_line.finance_book_adjustment_line import (
			FinanceBookAdjustmentLine,
		)

		amended_from: DF.Link | None
		company: DF.Link
		cost_center: DF.Link | None
		finance_book: DF.Link
		from_date: DF.Date
		journal_entry: DF.Link | None
		lines: DF.Table[FinanceBookAdjustmentLine]
		posting_date: DF.Date | None
		status: DF.Literal["Draft", "Computed", "Posted", "Cancelled"]
		to_date: DF.Date
		total_credit: DF.Currency
		total_debit: DF.Currency
	# end: auto-generated types

	def validate(self):
		self.validate_dates()
		self.reset_lines_if_inputs_changed()
		self.validate_no_overlap()
		self.set_totals()
		if self.docstatus.is_draft():
			self.status = "Computed" if self.lines else "Draft"

	def validate_dates(self):
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date cannot be after To Date."))
		if not self.posting_date:
			self.posting_date = self.to_date
		if getdate(self.posting_date) < getdate(self.to_date):
			frappe.throw(_("Posting Date cannot be before the To Date of the adjusted period."))

	def reset_lines_if_inputs_changed(self):
		"""Computed lines are stale as soon as company/book/period change."""
		if self.is_new() or not self.docstatus.is_draft():
			return
		if any(self.has_value_changed(fieldname) for fieldname in COMPUTE_INPUT_FIELDS):
			self.set("lines", [])

	def validate_no_overlap(self):
		"""One submitted adjustment per company + finance book + day.

		Together with the untagged-only movement base (see
		``loaders.get_account_movements``) this is the re-run protection: a
		period can only carry one posted adjustment JE per book, so movements
		can never be adjusted twice.
		"""
		others = frappe.get_all(
			"Finance Book Adjustment",
			filters={
				"docstatus": 1,
				"company": self.company,
				"finance_book": self.finance_book,
				"name": ("!=", self.name),
			},
			fields=["name", "from_date", "to_date"],
		)
		for other in others:
			if engine.periods_overlap(
				getdate(self.from_date), getdate(self.to_date), getdate(other.from_date), getdate(other.to_date)
			):
				frappe.throw(
					_(
						"The period {0} to {1} overlaps submitted Finance Book Adjustment {2} "
						"({3} to {4}) for the same company and finance book."
					).format(
						frappe.bold(self.from_date),
						frappe.bold(self.to_date),
						get_link_to_form("Finance Book Adjustment", other.name),
						other.from_date,
						other.to_date,
					),
					title=_("Overlapping Adjustment Period"),
				)

	def set_totals(self):
		self.total_debit = flt(sum(flt(line.debit) for line in self.lines), 2)
		self.total_credit = flt(sum(flt(line.credit) for line in self.lines), 2)

	@frappe.whitelist()
	def compute(self):
		"""Compute the adjustment lines from the enabled policy rules (draft only).

		Movements come from the common (untagged) GL layer only, so entries
		already tagged with this - or any other - finance book never feed the
		base and re-computing cannot compound earlier adjustments.
		"""
		if not self.docstatus.is_draft():
			frappe.throw(_("Adjustment lines can only be computed on a draft document."))

		rules = loaders.get_policy_rules(self.company, self.finance_book)
		if not rules:
			frappe.throw(
				_("No enabled Accounting Policy Rule found for company {0} and finance book {1}.").format(
					frappe.bold(self.company), frappe.bold(self.finance_book)
				)
			)

		source_accounts = sorted(
			{rule.source_account for rule in rules if rule.rule_type != MANUAL_AMOUNT and rule.source_account}
		)
		movements = loaders.get_account_movements(
			self.company, source_accounts, self.from_date, self.to_date
		)

		try:
			lines = engine.build_adjustment_lines(rules, movements)
		except ValueError as e:
			frappe.throw(str(e), title=_("Adjustment Computation Error"))

		self.set("lines", [])
		for line in lines:
			self.append(
				"lines",
				{
					"account": line.account,
					"debit": line.debit,
					"credit": line.credit,
					"source_rule": line.source_rules[0] if len(line.source_rules) == 1 else None,
				},
			)

		self.save()

		return {
			"lines": len(self.lines),
			"total_debit": self.total_debit,
			"total_credit": self.total_credit,
			"status": self.status,
		}

	def before_submit(self):
		if not self.lines:
			frappe.throw(
				_("Compute the adjustment lines before submitting; there is nothing to post.")
			)
		if flt(self.total_debit, 2) != flt(self.total_credit, 2):
			frappe.throw(
				_("Total debit {0} does not equal total credit {1}.").format(
					self.total_debit, self.total_credit
				)
			)
		self.validate_account_currency()

	def validate_account_currency(self):
		"""v1 posts in company currency only (see multibook/DESIGN.md)."""
		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for line in self.lines:
			account_currency = get_account_currency(line.account)
			if account_currency and account_currency != company_currency:
				frappe.throw(
					_(
						"Account {0} is denominated in {1}, not the company currency {2}. "
						"Multi-currency adjustment accounts are not supported yet."
					).format(frappe.bold(line.account), account_currency, company_currency)
				)

	def on_submit(self):
		journal_entry = self.make_journal_entry()
		self.db_set("journal_entry", journal_entry.name)
		self.db_set("status", "Posted")

	def make_journal_entry(self):
		"""One submitted, book-tagged Journal Entry for the computed lines.

		Because ``finance_book`` is stamped on the JE (and flows to its GL
		entries), the lines appear only in this book's report view -
		``finance_book IN (B, '') OR finance_book IS NULL`` - and never in
		other books or the default-book view (quoted in multibook/DESIGN.md).
		"""
		rule_names = {line.source_rule for line in self.lines if line.source_rule}
		if any(not line.source_rule for line in self.lines):
			# consolidated lines merge several rules; name every enabled rule
			rule_names |= {rule.name for rule in loaders.get_policy_rules(self.company, self.finance_book)}
		rule_names = sorted(rule_names)
		remark = _("Finance Book Adjustment {0} for finance book {1}, period {2} to {3}. Rules: {4}").format(
			self.name, self.finance_book, self.from_date, self.to_date, ", ".join(rule_names)
		)

		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Journal Entry"
		journal_entry.company = self.company
		journal_entry.posting_date = self.posting_date or self.to_date
		journal_entry.finance_book = self.finance_book
		journal_entry.user_remark = remark

		for line in self.lines:
			journal_entry.append(
				"accounts",
				{
					"account": line.account,
					"debit": flt(line.debit, 2),
					"debit_in_account_currency": flt(line.debit, 2),
					"credit": flt(line.credit, 2),
					"credit_in_account_currency": flt(line.credit, 2),
					"account_currency": get_account_currency(line.account),
					"cost_center": self.cost_center,
					"user_remark": remark,
				},
			)

		journal_entry.save()
		journal_entry.submit()
		return journal_entry

	def on_cancel(self):
		if self.journal_entry:
			if frappe.db.get_value("Journal Entry", self.journal_entry, "docstatus") == 1:
				frappe.get_doc("Journal Entry", self.journal_entry).cancel()
		self.db_set("status", "Cancelled")
