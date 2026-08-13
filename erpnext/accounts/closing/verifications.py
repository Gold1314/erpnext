# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Auto-verification of close tasks against the existing accounting doctypes.

Each verifier answers "is this step of the close actually done?" by reading
the doctype that *is* the evidence (Period Closing Voucher, Exchange Rate
Revaluation, ...) instead of trusting a checkbox. Every verifier has the
signature ``(company, period_start, period_end) -> (ok: bool, message: str)``
where ``period_start`` may be ``None`` (only the period end is mandatory on a
Close Cycle).

Fieldnames used below were verified against the target doctypes' JSON:

- Period Closing Voucher: ``company``, ``period_start_date``, ``period_end_date`` (submittable)
- Exchange Rate Revaluation: ``company``, ``posting_date`` (submittable)
- Process Deferred Accounting: ``company``, ``start_date``, ``end_date`` (submittable);
  Accounts Settings: ``automatically_process_deferred_accounting_entry``
- Bank Account: ``company``, ``is_company_account``, ``disabled``, ``account_name``
- Bank Transaction: ``bank_account``, ``date``, ``status``
  (Pending/Settled/Unreconciled/Reconciled/Cancelled), ``unallocated_amount`` (submittable)
- Ledger Health: ``debit_credit_mismatch``, ``general_and_payment_ledger_mismatch``
  (no company/period fields - see verifier docstring)
- Accounting Period: ``company``, ``start_date``, ``end_date``, ``disabled``,
  ``closed_documents``; Closed Document (child): ``closed``
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime

from erpnext.accounts.closing.sequencing import DONE_STATUSES

SUBMITTED = 1


def verify_period_closing_voucher(company, period_start, period_end):
	"""A submitted Period Closing Voucher exists for the company covering period end."""
	pcv = frappe.db.get_value(
		"Period Closing Voucher",
		{
			"docstatus": SUBMITTED,
			"company": company,
			"period_start_date": ("<=", period_end),
			"period_end_date": (">=", period_end),
		},
		"name",
	)
	if pcv:
		return True, _("Period Closing Voucher {0} covers {1}.").format(pcv, period_end)
	return False, _("No submitted Period Closing Voucher covers {0} for {1}.").format(period_end, company)


def verify_exchange_rate_revaluation(company, period_start, period_end):
	"""A submitted Exchange Rate Revaluation exists with posting_date inside the period."""
	if not period_start:
		return False, _(
			"Close Cycle has no period start date, so a revaluation cannot be confirmed as posted within the period."
		)

	filters = {
		"docstatus": SUBMITTED,
		"company": company,
		"posting_date": ("between", (period_start, period_end)),
	}
	err = frappe.db.get_value("Exchange Rate Revaluation", filters, "name")
	if err:
		return True, _("Exchange Rate Revaluation {0} was posted within the period.").format(err)
	return False, _("No submitted Exchange Rate Revaluation posted within the period for {0}.").format(company)


def verify_deferred_accounting(company, period_start, period_end):
	"""Deferred accounting handled: either the automatic scheduler is on, or a
	submitted Process Deferred Accounting run covers the period."""
	if cint(frappe.db.get_single_value("Accounts Settings", "automatically_process_deferred_accounting_entry")):
		return True, _("Accounts Settings processes deferred accounting entries automatically.")

	filters = {
		"docstatus": SUBMITTED,
		"company": company,
		"end_date": (">=", period_end),
		"start_date": ("<=", period_start or period_end),
	}
	pda = frappe.db.get_value("Process Deferred Accounting", filters, "name")
	if pda:
		return True, _("Process Deferred Accounting {0} covers the period.").format(pda)
	return False, _(
		"No submitted Process Deferred Accounting run covers the period for {0}, and automatic processing is off in Accounts Settings."
	).format(company)


def verify_bank_reconciliation(company, period_start, period_end):
	"""Every enabled company Bank Account has zero unreconciled Bank Transactions
	in the period (submitted, not Reconciled/Cancelled, with unallocated amount)."""
	if not period_start:
		return False, _(
			"Close Cycle has no period start date, so bank reconciliation cannot be scoped to the period."
		)

	bank_accounts = frappe.get_all(
		"Bank Account",
		filters={"company": company, "is_company_account": 1, "disabled": 0},
		fields=["name", "account_name"],
	)
	if not bank_accounts:
		return True, _("No enabled company bank accounts to reconcile for {0}.").format(company)

	pending = []
	for account in bank_accounts:
		filters = {
			"docstatus": SUBMITTED,
			"bank_account": account.name,
			"status": ("not in", ("Reconciled", "Cancelled")),
			"unallocated_amount": (">", 0),
			"date": ("between", (period_start, period_end)),
		}
		count = frappe.db.count("Bank Transaction", filters)
		if count:
			pending.append(f"{account.account_name or account.name}: {count}")

	if pending:
		return False, _("Unreconciled bank transactions in the period - {0}.").format("; ".join(pending))
	return True, _("All {0} company bank accounts are fully reconciled for the period.").format(
		len(bank_accounts)
	)


def verify_ledger_health(company, period_start, period_end):
	"""No Ledger Health rows flag a debit/credit or GL-vs-payment-ledger mismatch.

	Ledger Health rows carry only voucher_type/voucher_no and the mismatch
	flags - they are not scoped by company or posting date - so this check is
	site-wide: any open mismatch anywhere fails the task.
	"""
	mismatches = frappe.get_all(
		"Ledger Health",
		or_filters=[
			["debit_credit_mismatch", "=", 1],
			["general_and_payment_ledger_mismatch", "=", 1],
		],
		limit_page_length=0,
	)
	if mismatches:
		return False, _(
			"Ledger Health Monitor flagged {0} voucher(s) with debit/credit or ledger mismatches."
		).format(len(mismatches))
	return True, _("Ledger Health Monitor reports no debit/credit or ledger mismatches.")


def verify_accounting_period_lock(company, period_start, period_end):
	"""An enabled Accounting Period covers the period and closes at least one document type."""
	periods = frappe.get_all(
		"Accounting Period",
		filters={
			"company": company,
			"disabled": 0,
			"start_date": ("<=", period_start or period_end),
			"end_date": (">=", period_end),
		},
		pluck="name",
	)
	if not periods:
		return False, _("No enabled Accounting Period covers the period for {0}.").format(company)

	closed = frappe.get_all(
		"Closed Document",
		filters={"parenttype": "Accounting Period", "parent": ("in", periods), "closed": 1},
		pluck="document_type",
	)
	if not closed:
		return False, _(
			"Accounting Period {0} covers the period but has no document types marked closed."
		).format(periods[0])
	return True, _("Accounting Period {0} locks {1} document type(s) for the period.").format(
		periods[0], len(set(closed))
	)


def verify_manual(company, period_start, period_end):
	"""Manual tasks are never auto-verified - a person must sign off in the UI."""
	return False, _("Requires manual sign-off.")


TASK_TYPE_VERIFIERS = {
	"Period Closing Voucher": verify_period_closing_voucher,
	"Exchange Rate Revaluation": verify_exchange_rate_revaluation,
	"Deferred Accounting": verify_deferred_accounting,
	"Bank Reconciliation": verify_bank_reconciliation,
	"Ledger Health": verify_ledger_health,
	"Accounting Period Lock": verify_accounting_period_lock,
	"Manual": verify_manual,
}


def run_verification(task_type, company, period_start, period_end):
	"""Dispatch to the verifier for ``task_type``. Returns ``(ok, message)``."""
	verifier = TASK_TYPE_VERIFIERS.get(task_type)
	if not verifier:
		return False, _("No verifier registered for task type {0}.").format(task_type)
	period_start = getdate(period_start) if period_start else None
	return verifier(company, period_start, getdate(period_end))


def verify_task(task, cycle=None):
	"""Run the verification for one Close Task document and persist the result.

	Stores the (Passed/Failed-prefixed) message in ``verification_result``. When
	the check passes, the task is auto-verifiable, and no dependency blocks it,
	the task is marked Completed (the controller stamps ``signed_off_by`` with
	``frappe.session.user`` and ``signed_off_on``). A failed check never changes
	the status - only dependency logic governs Blocked.

	Returns ``(ok, message)``.
	"""
	cycle = cycle or frappe.get_doc("Close Cycle", task.close_cycle)
	ok, message = run_verification(
		task.task_type, cycle.company, cycle.period_start_date, cycle.period_end_date
	)

	completed = False
	if ok:
		task.verification_result = _("Passed ({0}): {1}").format(now_datetime(), message)
		if cint(task.auto_verify) and task.status not in DONE_STATUSES:
			if task.get_blocking_dependencies():
				task.verification_result += " " + _("(Not auto-completed: blocked by open dependencies.)")
			else:
				task.status = "Completed"
				completed = True
	else:
		task.verification_result = _("Failed ({0}): {1}").format(now_datetime(), message)

	task.save()
	return ok, message if not completed else message + " " + _("Task marked Completed.")


@frappe.whitelist()
def run_auto_verifications(close_cycle):
	"""Run all pending auto-verifiable tasks of a Close Cycle.

	Iterates tasks with ``auto_verify`` enabled that are still Pending or
	In Progress; each passing check marks its task Completed (unless blocked
	by open dependencies), each failing check just records the failure
	message. Returns a summary dict for the UI.
	"""
	frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))

	cycle = frappe.get_doc("Close Cycle", close_cycle)
	task_names = frappe.get_all(
		"Close Task",
		filters={
			"close_cycle": cycle.name,
			"auto_verify": 1,
			"status": ("in", ("Pending", "In Progress")),
		},
		order_by="due_date asc, name asc",
		pluck="name",
	)

	results = []
	completed = failed = 0
	for name in task_names:
		task = frappe.get_doc("Close Task", name)
		ok, message = verify_task(task, cycle)
		if ok and task.status == "Completed":
			completed += 1
		elif not ok:
			failed += 1
		results.append(
			{"task": task.name, "task_title": task.task_title, "ok": ok, "message": message}
		)

	return {
		"tasks_checked": len(task_names),
		"completed": completed,
		"failed": failed,
		"results": results,
	}
