# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext.accounts.closing.sequencing import resolve_dependencies


class CloseTaskTemplate(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.close_task_template_item.close_task_template_item import (
			CloseTaskTemplateItem,
		)

		company: DF.Link | None
		description: DF.SmallText | None
		tasks: DF.Table[CloseTaskTemplateItem]
		template_name: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_task_graph()

	def validate_task_graph(self):
		"""Titles must be unique and the depends_on_titles graph acyclic."""
		rows = [
			{"title": row.task_title, "depends_on_titles": row.depends_on_titles} for row in self.tasks
		]
		try:
			resolve_dependencies(rows)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid Task Dependencies"))


@frappe.whitelist()
def create_default_close_template():
	"""Idempotently install the shipped "Standard Monthly Close" template."""
	frappe.only_for(("System Manager", "Accounts Manager"))

	template_name = "Standard Monthly Close"
	if frappe.db.exists("Close Task Template", template_name):
		return {"created": [], "skipped": [template_name]}

	doc = frappe.new_doc("Close Task Template")
	doc.template_name = template_name
	doc.description = _(
		"Shipped month-end close checklist: reconcile banks, revalue currency, process deferred "
		"accounting, review subledgers, verify ledger health, post the Period Closing Voucher and "
		"lock the period."
	)
	for task in get_default_close_tasks():
		doc.append("tasks", task)
	doc.insert(ignore_permissions=True)

	return {"created": [template_name], "skipped": []}


def get_default_close_tasks():
	"""Task pack built only from roles that ship with ERPNext."""
	return [
		{
			"task_title": "Reconcile Bank Accounts",
			"task_type": "Bank Reconciliation",
			"depends_on_titles": "",
			"owner_role": "Accounts User",
			"due_day_offset": 2,
			"auto_verify": 1,
			"instructions": "Match all bank transactions for the period using the Bank Reconciliation tool. The task auto-verifies when no company bank account has unreconciled transactions in the period.",
		},
		{
			"task_title": "Run Exchange Rate Revaluation",
			"task_type": "Exchange Rate Revaluation",
			"depends_on_titles": "Reconcile Bank Accounts",
			"owner_role": "Accounts Manager",
			"due_day_offset": 3,
			"auto_verify": 1,
			"instructions": "Revalue foreign-currency balances as of the period end and submit the Exchange Rate Revaluation.",
		},
		{
			"task_title": "Process Deferred Accounting",
			"task_type": "Deferred Accounting",
			"depends_on_titles": "",
			"owner_role": "Accounts User",
			"due_day_offset": 3,
			"auto_verify": 1,
			"instructions": "Run Process Deferred Accounting for the period (or rely on automatic processing in Accounts Settings).",
		},
		{
			"task_title": "Review AR Ageing",
			"task_type": "Manual",
			"depends_on_titles": "Reconcile Bank Accounts",
			"owner_role": "Accounts User",
			"due_day_offset": 4,
			"auto_verify": 0,
			"instructions": "Review the Accounts Receivable report for overdue balances, disputed invoices and required provisions. Attach the reviewed report as evidence.",
		},
		{
			"task_title": "Review AP Ageing",
			"task_type": "Manual",
			"depends_on_titles": "Reconcile Bank Accounts",
			"owner_role": "Accounts User",
			"due_day_offset": 4,
			"auto_verify": 0,
			"instructions": "Review the Accounts Payable report for missed invoices, debit balances and accrual needs. Attach the reviewed report as evidence.",
		},
		{
			"task_title": "Review Stock Valuation",
			"task_type": "Manual",
			"depends_on_titles": "",
			"owner_role": "Accounts User",
			"due_day_offset": 4,
			"auto_verify": 0,
			"instructions": "Compare the Stock Balance report against the stock ledger accounts and investigate valuation differences.",
		},
		{
			"task_title": "Verify Ledger Health",
			"task_type": "Ledger Health",
			"depends_on_titles": "Run Exchange Rate Revaluation, Process Deferred Accounting",
			"owner_role": "Accounts Manager",
			"due_day_offset": 5,
			"auto_verify": 1,
			"instructions": "Ledger Health Monitor must report no debit/credit or general-vs-payment ledger mismatches.",
		},
		{
			"task_title": "Post Period Closing Voucher",
			"task_type": "Period Closing Voucher",
			"depends_on_titles": "Verify Ledger Health, Review AR Ageing, Review AP Ageing, Review Stock Valuation",
			"owner_role": "Accounts Manager",
			"due_day_offset": 6,
			"auto_verify": 1,
			"instructions": "Submit the Period Closing Voucher to move P&L balances to retained earnings.",
		},
		{
			"task_title": "Management Sign-off",
			"task_type": "Manual",
			"depends_on_titles": "Post Period Closing Voucher",
			"owner_role": "Accounts Manager",
			"due_day_offset": 7,
			"auto_verify": 0,
			"instructions": "Finance leadership reviews the final statements for the period and signs off the close.",
		},
		{
			"task_title": "Lock Accounting Period",
			"task_type": "Accounting Period Lock",
			"depends_on_titles": "Management Sign-off",
			"owner_role": "Accounts Manager",
			"due_day_offset": 7,
			"auto_verify": 1,
			"instructions": "Create or enable an Accounting Period covering the close period with the relevant document types marked closed.",
		},
	]
