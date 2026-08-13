# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, getdate, nowdate

from erpnext.accounts.doctype.ssp_price.ssp_price import get_ssp
from erpnext.accounts.revenue import engine
from erpnext.accounts.revenue.models import OVER_TIME, POINT_IN_TIME, ObligationInput
from erpnext.accounts.utils import get_account_currency


class RevenueContract(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.revenue_contract_obligation.revenue_contract_obligation import (
			RevenueContractObligation,
		)
		from erpnext.accounts.doctype.revenue_recognition_entry.revenue_recognition_entry import (
			RevenueRecognitionEntry,
		)

		amended_from: DF.Link | None
		auto_post_monthly: DF.Check
		company: DF.Link
		contract_date: DF.Date
		cost_center: DF.Link | None
		customer: DF.Link
		deferred_revenue_account: DF.Link
		obligations: DF.Table[RevenueContractObligation]
		periodicity: DF.Literal["Monthly"]
		recognition_plan: DF.Table[RevenueRecognitionEntry]
		source_doctype: DF.Literal["", "Sales Order", "Sales Invoice"]
		source_name: DF.DynamicLink | None
		status: DF.Literal["Draft", "Active", "Completed", "Cancelled"]
		transaction_price: DF.Currency
	# end: auto-generated types

	def validate(self):
		if self.docstatus.is_draft():
			self.set_default_ssp()
			self.compute_allocation_and_plan()

	def set_default_ssp(self):
		"""Default each obligation's SSP from the SSP Price catalog (editable)."""
		for obligation in self.obligations:
			if not flt(obligation.ssp) and obligation.item_code:
				catalog_ssp = get_ssp(obligation.item_code, self.company, self.contract_date)
				if catalog_ssp is not None:
					obligation.ssp = catalog_ssp

	def get_obligation_inputs(self) -> list[ObligationInput]:
		inputs = []
		for obligation in self.obligations:
			inputs.append(
				ObligationInput(
					key=str(obligation.idx),
					description=obligation.description,
					stated_amount=flt(obligation.stated_amount),
					ssp=flt(obligation.ssp),
					satisfaction_method=obligation.satisfaction_method,
					start_date=getdate(obligation.service_start_date)
					if obligation.service_start_date
					else None,
					end_date=getdate(obligation.service_end_date) if obligation.service_end_date else None,
					satisfied_date=getdate(obligation.satisfied_date) if obligation.satisfied_date else None,
				)
			)
		return inputs

	def compute_allocation_and_plan(self):
		"""Steps 3-5: transaction price, relative-SSP allocation, recognition plan.

		Draft-only (called from validate): writes allocated amounts / percents
		on the obligations and regenerates the recognition_plan child rows.
		"""
		self.transaction_price = flt(sum(flt(o.stated_amount) for o in self.obligations), 2)

		if not self.obligations:
			self.set("recognition_plan", [])
			return

		try:
			plan = engine.build_recognition_plan(
				self.transaction_price,
				self.get_obligation_inputs(),
				periodicity=self.periodicity or "Monthly",
			)
		except ValueError as e:
			frappe.throw(str(e), title=_("Revenue Allocation Error"))

		allocated_by_key = {a.key: a for a in plan.allocations}
		description_by_key = {}
		for obligation in self.obligations:
			allocation = allocated_by_key.get(str(obligation.idx))
			obligation.allocated_amount = allocation.allocated_amount if allocation else 0
			obligation.allocation_pct = allocation.allocation_pct if allocation else 0
			description_by_key[str(obligation.idx)] = obligation.description

		self.set("recognition_plan", [])
		for row in plan.recognition_rows:
			self.append(
				"recognition_plan",
				{
					"obligation_idx": int(row.key),
					"obligation_description": description_by_key.get(row.key),
					"period_start": row.period_start,
					"period_end": row.period_end,
					"amount": row.amount,
					"posted": 0,
				},
			)

	def on_submit(self):
		plan_total = flt(sum(flt(row.amount) for row in self.recognition_plan), 2)
		if plan_total != flt(self.transaction_price, 2):
			frappe.throw(
				_("Recognition plan total {0} does not equal the transaction price {1}.").format(
					plan_total, self.transaction_price
				),
				title=_("Plan Out of Balance"),
			)
		self.db_set("status", "Active")

	def on_cancel(self):
		posted_rows = [row for row in self.recognition_plan if row.posted]
		if posted_rows:
			frappe.throw(
				_(
					"Cannot cancel: {0} recognition entries have already been posted. "
					"Cancel the linked Journal Entries first."
				).format(len(posted_rows))
			)
		self.db_set("status", "Cancelled")

	def get_obligation_by_idx(self, obligation_idx: int):
		for obligation in self.obligations:
			if obligation.idx == obligation_idx:
				return obligation
		frappe.throw(_("No obligation at row {0}.").format(obligation_idx))

	@frappe.whitelist()
	def load_from_source(self):
		"""Pull performance obligations from the linked Sales Order / Sales Invoice.

		One obligation per item row: ``base_net_amount`` becomes the stated
		amount, SSP defaults from the SSP Price catalog (falling back to the
		stated amount, reported via msgprint), and service dates (Sales
		Invoice only) select the Over Time method.
		"""
		if not self.docstatus.is_draft():
			frappe.throw(_("Obligations can only be loaded on a draft contract."))
		if not (self.source_doctype and self.source_name):
			frappe.throw(_("Select a Source Document Type and Source Document first."))

		source = frappe.get_doc(self.source_doctype, self.source_name)

		if not self.company:
			self.company = source.company
		if not self.customer:
			self.customer = source.customer
		if not self.cost_center and source.get("cost_center"):
			self.cost_center = source.cost_center
		if not self.contract_date:
			self.contract_date = (
				source.posting_date if self.source_doctype == "Sales Invoice" else source.transaction_date
			)

		default_income_account = frappe.get_cached_value("Company", self.company, "default_income_account")

		ssp_fallbacks = []
		income_account_fallbacks = []
		self.set("obligations", [])

		for item in source.items:
			stated_amount = flt(item.base_net_amount)

			ssp = get_ssp(item.item_code, self.company, self.contract_date) if item.item_code else None
			if ssp is None:
				ssp = stated_amount
				ssp_fallbacks.append(item.item_code or item.description)

			service_start = item.get("service_start_date")
			service_end = item.get("service_end_date")

			income_account = item.get("income_account")
			if not income_account:
				income_account = default_income_account
				income_account_fallbacks.append(item.item_code or item.description)

			if (
				self.source_doctype == "Sales Invoice"
				and not self.deferred_revenue_account
				and item.get("enable_deferred_revenue")
			):
				self.deferred_revenue_account = item.deferred_revenue_account

			self.append(
				"obligations",
				{
					"item_code": item.item_code,
					"description": item.description or item.item_code,
					"stated_amount": stated_amount,
					"ssp": ssp,
					"satisfaction_method": OVER_TIME if (service_start and service_end) else POINT_IN_TIME,
					"service_start_date": service_start,
					"service_end_date": service_end,
					"income_account": income_account,
				},
			)

		if ssp_fallbacks:
			frappe.msgprint(
				_("No SSP Price found for {0}; the stated amount was used as SSP.").format(
					", ".join(frappe.bold(item) for item in ssp_fallbacks)
				),
				title=_("SSP Fallback"),
				indicator="orange",
			)
		if income_account_fallbacks:
			frappe.msgprint(
				_("No income account on the source rows for {0}; the company default was used.").format(
					", ".join(frappe.bold(item) for item in income_account_fallbacks)
				),
				indicator="orange",
			)

		self.save()

	@frappe.whitelist()
	def mark_obligation_satisfied(self, obligation_idx, satisfied_date=None):
		"""Record the satisfying event of a Point in Time obligation.

		Stamps the obligation and dates its (single) recognition plan row so
		``post_recognition`` can pick it up.
		"""
		if self.docstatus.is_draft():
			frappe.throw(_("Submit the contract before marking obligations satisfied."))
		if self.docstatus.is_cancelled():
			frappe.throw(_("This contract is cancelled."))

		obligation = self.get_obligation_by_idx(frappe.utils.cint(obligation_idx))
		if obligation.satisfaction_method != POINT_IN_TIME:
			frappe.throw(
				_("Obligation {0} ({1}) is recognized over time, not on an event.").format(
					obligation.idx, obligation.description
				)
			)
		if obligation.satisfied:
			frappe.throw(
				_("Obligation {0} ({1}) is already satisfied.").format(obligation.idx, obligation.description)
			)

		satisfied_date = getdate(satisfied_date or nowdate())
		obligation.db_set("satisfied", 1)
		obligation.db_set("satisfied_date", satisfied_date)

		for row in self.recognition_plan:
			if row.obligation_idx == obligation.idx and not row.posted:
				row.db_set("period_start", satisfied_date)
				row.db_set("period_end", satisfied_date)

		return {"obligation_idx": obligation.idx, "satisfied_date": str(satisfied_date)}

	@frappe.whitelist()
	def post_recognition(self, until_date=None):
		"""Post due recognition plan rows out of deferral (pattern: close/lease runs).

		For every unposted row with ``period_end <= until_date`` (default:
		today) - and, for Point in Time obligations, only once the obligation
		is marked satisfied - one submitted Journal Entry is created:

			Dr Deferred Revenue (contract-level liability account)
			Cr obligation.income_account

		This module recognizes *out of* deferral; billing must credit the
		deferred revenue account (standard ERPNext deferred flow or a manual
		entry) - see DESIGN.md.
		"""
		if not self.docstatus.is_submitted():
			frappe.throw(_("Recognition can only be posted on a submitted contract."))

		until_date = getdate(until_date or nowdate())
		obligations_by_idx = {o.idx: o for o in self.obligations}
		posted_entries = []
		skipped_pending_event = 0

		for row in self.recognition_plan:
			if row.posted:
				continue

			obligation = obligations_by_idx.get(row.obligation_idx)
			if not obligation:
				continue

			if obligation.satisfaction_method == POINT_IN_TIME and not obligation.satisfied:
				skipped_pending_event += 1
				continue

			if not row.period_end or getdate(row.period_end) > until_date:
				continue

			if flt(row.amount):
				journal_entry = self.make_recognition_journal_entry(row, obligation)
				row.db_set("journal_entry", journal_entry.name)
				posted_entries.append(journal_entry.name)

			row.db_set("posted", 1)
			obligation.db_set(
				"recognized_amount", flt(flt(obligation.recognized_amount) + flt(row.amount), 2)
			)

		if all(row.posted for row in self.recognition_plan):
			self.db_set("status", "Completed")

		return {
			"posted": len(posted_entries),
			"journal_entries": [get_link_to_form("Journal Entry", name) for name in posted_entries],
			"pending_event": skipped_pending_event,
			"status": self.status,
		}

	def make_recognition_journal_entry(self, row, obligation):
		"""One submitted JE per plan row: Dr deferred revenue / Cr income.

		Construction mirrors ``deferred_revenue.book_revenue_via_journal_entry``
		(same voucher type and account-row shape); amounts are in company
		currency (v1 assumption, see DESIGN.md).
		"""
		amount = flt(row.amount, 2)
		posting_date = getdate(row.period_end)
		remark = _("Revenue recognition for {0}, obligation {1} ({2}), period {3} to {4}").format(
			self.name, obligation.idx, obligation.description, row.period_start, row.period_end
		)

		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.posting_date = posting_date
		journal_entry.company = self.company
		journal_entry.voucher_type = "Deferred Revenue"
		journal_entry.user_remark = remark

		debit_entry = {
			"account": self.deferred_revenue_account,
			"debit": amount,
			"debit_in_account_currency": amount,
			"account_currency": get_account_currency(self.deferred_revenue_account),
			"cost_center": self.cost_center,
			"user_remark": remark,
		}
		credit_entry = {
			"account": obligation.income_account,
			"credit": amount,
			"credit_in_account_currency": amount,
			"account_currency": get_account_currency(obligation.income_account),
			"cost_center": self.cost_center,
			"user_remark": remark,
		}

		journal_entry.append("accounts", debit_entry)
		journal_entry.append("accounts", credit_entry)

		journal_entry.save()
		journal_entry.submit()
		return journal_entry


def process_revenue_recognition():
	"""Monthly scheduler entry point.

	Posts due recognition entries for Active Revenue Contracts that opted in via
	``auto_post_monthly``. Opt-in is per contract and off by default, mirroring
	the lease module: revenue should not begin recognising itself on a timer
	without an explicit decision. Errors on one contract never stop the rest.
	"""
	contracts = frappe.get_all(
		"Revenue Contract",
		filters={"docstatus": 1, "status": "Active", "auto_post_monthly": 1},
		pluck="name",
	)

	for name in contracts:
		try:
			frappe.get_doc("Revenue Contract", name).post_recognition()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Scheduled revenue recognition failed for {name}",
				message=frappe.get_traceback(with_context=True),
			)
