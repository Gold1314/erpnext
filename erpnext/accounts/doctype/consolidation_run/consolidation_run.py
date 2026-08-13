# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Consolidation Run — a *reporting-layer* document.

It computes and stores currency translation, intercompany eliminations and
minority interest for a group, but posts **nothing** to the General Ledger:
submitting a run only freezes ("Finalizes") the stored adjustment lines that
the "Consolidated Statement with Eliminations" report renders. Statutory
books of the individual companies are never touched.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from erpnext.accounts.consolidation import engine, loaders


class ConsolidationRun(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.consolidation_adjustment_line.consolidation_adjustment_line import (
			ConsolidationAdjustmentLine,
		)

		adjustment_lines: DF.Table[ConsolidationAdjustmentLine]
		amended_from: DF.Link | None
		exceptions: DF.SmallText | None
		from_date: DF.Date
		minority_interest_total: DF.Currency
		parent_company: DF.Link
		presentation_currency: DF.Link
		report_date: DF.Date | None
		status: DF.Literal["Draft", "Computed", "Finalized", "Cancelled"]
		to_date: DF.Date
	# end: auto-generated types

	def validate(self):
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date cannot be after To Date."))
		if not self.report_date:
			self.report_date = self.to_date
		if not self.status:
			self.status = "Draft"

	def on_submit(self):
		if self.status != "Computed":
			frappe.throw(_("Compute the consolidation before submitting."))
		# Finalized = the stored adjustments are frozen for reporting.
		# Deliberately posts NOTHING to the GL — consolidation is a
		# reporting layer over the companies' statutory books.
		self.db_set("status", "Finalized")

	def on_cancel(self):
		self.db_set("status", "Cancelled")

	@frappe.whitelist()
	def compute(self):
		"""Run loaders + pure engine and store the results on this document."""
		if self.docstatus != 0:
			frappe.throw(_("Only draft Consolidation Runs can be computed."))

		result = self.run_engine()

		self.set("adjustment_lines", [])
		self.append_elimination_lines(result)
		self.append_cta_lines(result)
		self.append_minority_interest_lines(result)

		messages = [*result.exceptions, *result.warnings]
		self.exceptions = "\n".join(messages)
		self.minority_interest_total = flt(result.minority_interest_total)
		self.status = "Computed"
		self.save()

		return {
			"companies": result.companies,
			"exceptions": result.exceptions,
			"warnings": result.warnings,
			"minority_interest_total": self.minority_interest_total,
		}

	def run_engine(self):
		report_date = self.report_date or self.to_date
		edges = loaders.get_ownership_edges(self.parent_company, report_date)

		try:
			members = engine.resolve_group(self.parent_company, edges)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid Ownership Structure"))

		companies = [self.parent_company] + [
			m.company for m in members.values() if m.method == engine.METHOD_FULL
		]

		balances = loaders.get_balances(companies, self.from_date, self.to_date, report_date)
		rates, rate_warnings = loaders.get_rates(
			companies, self.presentation_currency, report_date, self.from_date, self.to_date
		)
		pairs = loaders.get_elimination_pairs(self.parent_company)
		abbrs = loaders.get_company_abbrs(companies)

		try:
			result = engine.consolidate(self.parent_company, balances, edges, rates, pairs, abbrs)
		except ValueError as e:
			frappe.throw(str(e), title=_("Consolidation Failed"))

		result.warnings.extend(rate_warnings)
		return result

	def append_elimination_lines(self, result):
		for line in result.elimination_lines:
			self.append(
				"adjustment_lines",
				{
					"line_type": "Elimination",
					"company": line.company,
					"account": line.account,
					"debit": line.amount if line.amount > 0 else 0.0,
					"credit": -line.amount if line.amount < 0 else 0.0,
					"rule": line.rule,
				},
			)

	def append_cta_lines(self, result):
		from erpnext.accounts.consolidation.models import CTA_ACCOUNT

		for company, amount in result.cta_by_company.items():
			if not amount:
				continue
			self.append(
				"adjustment_lines",
				{
					"line_type": "CTA",
					"company": company,
					"account": CTA_ACCOUNT,
					"debit": amount if amount > 0 else 0.0,
					"credit": -amount if amount < 0 else 0.0,
					"rule": _("Currency translation plug"),
				},
			)

	def append_minority_interest_lines(self, result):
		"""MI is stored as a net-zero reclass inside equity: credit the
		"Minority Interest" line, debit the offsetting group-equity reclass —
		so the consolidated balance sheet stays balanced."""
		from erpnext.accounts.consolidation.models import (
			MI_RECLASS_ACCOUNT,
			MINORITY_INTEREST_ACCOUNT,
		)

		for company, amount in result.minority_interest_by_company.items():
			# amount is credit-positive
			self.append(
				"adjustment_lines",
				{
					"line_type": "Minority Interest",
					"company": company,
					"account": MINORITY_INTEREST_ACCOUNT,
					"debit": -amount if amount < 0 else 0.0,
					"credit": amount if amount > 0 else 0.0,
					"rule": _("Outside share of equity + current net income"),
				},
			)
			self.append(
				"adjustment_lines",
				{
					"line_type": "Minority Interest",
					"company": company,
					"account": MI_RECLASS_ACCOUNT,
					"debit": amount if amount > 0 else 0.0,
					"credit": -amount if amount < 0 else 0.0,
					"rule": _("Reclass within group equity"),
				},
			)
