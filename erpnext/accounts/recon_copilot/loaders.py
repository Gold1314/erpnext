# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the reconciliation copilot.

All DB access lives here; the ranking engine (``engine.py``) stays pure.

The candidate universe deliberately **mirrors the existing reconciliation
tool's queries** in
``erpnext/accounts/doctype/bank_reconciliation_tool/bank_reconciliation_tool.py``
so the copilot only re-ranks what the tool would surface, never invents a new
voucher universe:

- Payment Entries    mirrors ``get_pe_matching_query`` (docstatus 1, payment
  type Receive/Pay by direction + Internal Transfer, ``clearance_date`` IS
  NULL, paid_to/paid_from = the bank GL account).
- Journal Entries    mirrors ``get_je_matching_query`` (docstatus 1, not an
  Opening Entry, ``clearance_date`` IS NULL, a Journal Entry Account row on
  the bank GL account, amount = sum of credit/debit in account currency by
  direction).
- Sales Invoices     deposits only, mirrors ``get_si_matching_query`` (POS
  payment rows: Sales Invoice Payment with ``clearance_date`` IS NULL and
  ``account`` = bank GL account, invoice currency = bank account currency).
- Purchase Invoices  withdrawals only, mirrors ``get_pi_matching_query``
  (``is_paid`` = 1, ``clearance_date`` IS NULL, ``cash_bank_account`` = bank
  GL account, currency match).

Amounts already allocated to other bank transactions are subtracted by
delegating to the tool's own ``subtract_allocations``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.custom import ConstantColumn
from frappe.query_builder.functions import Max, Sum
from frappe.utils import add_days, flt, getdate

from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
	subtract_allocations,
)
from erpnext.accounts.recon_copilot.engine import description_prefix
from erpnext.accounts.recon_copilot.models import CandidateVoucher, MatchHistory, TxnFeatures
from erpnext.accounts.utils import get_account_currency

#: hard cap of rows fetched per voucher query - keeps request-thread work
#: bounded on large sites (the tool itself lets the user narrow by date; we
#: rank, so we prefer recent-first truncation over unbounded scans)
MAX_ROWS_PER_QUERY = 500

VOUCHER_TYPES = ("Payment Entry", "Journal Entry", "Sales Invoice", "Purchase Invoice")


# ---------------------------------------------------------------------------
# Transaction features
# ---------------------------------------------------------------------------


def get_transaction_features(bank_transaction_name: str) -> TxnFeatures:
	"""Build engine features for one Bank Transaction.

	``amount`` is the **unallocated** amount - the same figure the tool
	matches against (``check_matching``'s ``common_filters.amount``).
	``party_hint`` prefers the resolved party's display name, then the raw
	party id, then the statement's own counterparty (``bank_party_name``).
	"""
	txn = frappe.db.get_value(
		"Bank Transaction",
		bank_transaction_name,
		[
			"name",
			"date",
			"deposit",
			"withdrawal",
			"currency",
			"description",
			"reference_number",
			"party_type",
			"party",
			"bank_party_name",
			"unallocated_amount",
			"status",
			"bank_account",
			"company",
		],
		as_dict=True,
	)
	if not txn:
		frappe.throw(_("Bank Transaction {0} not found").format(bank_transaction_name))

	party_hint = txn.bank_party_name
	if txn.party_type and txn.party:
		party_hint = _get_party_display_name(txn.party_type, txn.party) or txn.party

	return TxnFeatures(
		amount=flt(txn.unallocated_amount),
		date=getdate(txn.date) if txn.date else None,
		description=txn.description,
		reference_number=txn.reference_number,
		party_hint=party_hint,
	)


def _get_party_display_name(party_type: str, party: str) -> str | None:
	name_field = {"Customer": "customer_name", "Supplier": "supplier_name", "Employee": "employee_name"}.get(
		party_type
	)
	if not name_field:
		return party
	return frappe.db.get_value(party_type, party, name_field)


# ---------------------------------------------------------------------------
# Candidates (mirror of the reconciliation tool's matching queries)
# ---------------------------------------------------------------------------


def get_candidates(bank_transaction, company: str | None = None) -> list[CandidateVoucher]:
	"""Unreconciled-side vouchers for a Bank Transaction (name or doc).

	Voucher universe and filters mirror ``bank_reconciliation_tool.py``:
	``get_pe_matching_query``, ``get_je_matching_query``,
	``get_si_matching_query`` (deposits), ``get_pi_matching_query``
	(withdrawals). Prior bank-transaction allocations are subtracted via the
	tool's ``subtract_allocations``.
	"""
	if isinstance(bank_transaction, str):
		transaction = frappe.db.get_value(
			"Bank Transaction",
			bank_transaction,
			["name", "deposit", "withdrawal", "bank_account"],
			as_dict=True,
		)
	else:
		transaction = bank_transaction

	bank = frappe.db.get_value("Bank Account", transaction.bank_account, ["account", "company"], as_dict=True)
	gl_account = bank.account
	currency = get_account_currency(gl_account)
	is_deposit = flt(transaction.deposit) > 0.0

	rows = []
	rows += _get_payment_entry_rows(gl_account, is_deposit)
	rows += _get_journal_entry_rows(gl_account, is_deposit)
	if is_deposit:
		rows += _get_sales_invoice_rows(gl_account, currency)
	else:
		rows += _get_purchase_invoice_rows(gl_account, currency)

	# same post-processing as get_linked_payments -> subtract_allocations
	rows = subtract_allocations(gl_account, rows)

	candidates = []
	for row in rows:
		amount = flt(row.get("paid_amount"))
		if amount <= 0:
			continue
		candidates.append(
			CandidateVoucher(
				voucher_type=row["doctype"],
				voucher_name=row["name"],
				amount=amount,
				date=getdate(row["posting_date"]) if row.get("posting_date") else None,
				party=row.get("party"),
				party_name=row.get("party_name") or row.get("party"),
				reference_no=row.get("reference_no") or None,
				bill_no=row.get("bill_no") or None,
				currency=row.get("currency"),
				outstanding=flt(row["outstanding"]) if row.get("outstanding") is not None else None,
			)
		)
	return candidates


def _get_payment_entry_rows(gl_account: str, is_deposit: bool) -> list[dict]:
	# mirrors get_pe_matching_query: docstatus 1, payment_type by direction or
	# Internal Transfer, clearance_date IS NULL, paid_to/paid_from = gl account
	account_from_to = "paid_to" if is_deposit else "paid_from"
	payment_type = "Receive" if is_deposit else "Pay"
	currency_field = "paid_to_account_currency" if is_deposit else "paid_from_account_currency"

	pe = frappe.qb.DocType("Payment Entry")
	return (
		frappe.qb.from_(pe)
		.select(
			ConstantColumn("Payment Entry").as_("doctype"),
			pe.name,
			pe.base_paid_amount_after_tax.as_("paid_amount"),
			pe.reference_no,
			pe.party,
			pe.party_name,
			pe.posting_date,
			getattr(pe, currency_field).as_("currency"),
		)
		.where(pe.docstatus == 1)
		.where(pe.payment_type.isin([payment_type, "Internal Transfer"]))
		.where(pe.clearance_date.isnull())
		.where(getattr(pe, account_from_to) == gl_account)
		.where(pe.paid_amount > 0.0)
		.orderby(pe.posting_date, order=Order.desc)
		.limit(MAX_ROWS_PER_QUERY)
	).run(as_dict=True)


def _get_journal_entry_rows(gl_account: str, is_deposit: bool) -> list[dict]:
	# mirrors get_je_matching_query: docstatus 1, voucher_type != Opening
	# Entry, clearance_date IS NULL, JE Account row on the bank GL account,
	# amount = sum(credit/debit in account currency) by direction
	cr_or_dr = "debit" if is_deposit else "credit"
	amount_field = f"{cr_or_dr}_in_account_currency"

	je = frappe.qb.DocType("Journal Entry")
	jea = frappe.qb.DocType("Journal Entry Account")
	rows = (
		frappe.qb.from_(jea)
		.join(je)
		.on(jea.parent == je.name)
		.select(
			ConstantColumn("Journal Entry").as_("doctype"),
			je.name,
			Sum(getattr(jea, amount_field)).as_("paid_amount"),
			Max(je.cheque_no).as_("reference_no"),
			Max(je.pay_to_recd_from).as_("party"),
			Max(je.posting_date).as_("posting_date"),
			Max(jea.account_currency).as_("currency"),
		)
		.where(je.docstatus == 1)
		.where(je.voucher_type != "Opening Entry")
		.where(je.clearance_date.isnull())
		.where(jea.account == gl_account)
		.groupby(je.name)
		.orderby(Max(je.posting_date), order=Order.desc)
		.limit(MAX_ROWS_PER_QUERY)
	).run(as_dict=True)

	return [row for row in rows if flt(row.get("paid_amount")) > 0.0]


def _get_sales_invoice_rows(gl_account: str, currency: str) -> list[dict]:
	# mirrors get_si_matching_query (deposits only): POS payment rows with
	# clearance_date IS NULL on the bank GL account, invoice currency match
	si = frappe.qb.DocType("Sales Invoice")
	sip = frappe.qb.DocType("Sales Invoice Payment")
	return (
		frappe.qb.from_(sip)
		.join(si)
		.on(sip.parent == si.name)
		.select(
			ConstantColumn("Sales Invoice").as_("doctype"),
			si.name,
			sip.amount.as_("paid_amount"),
			sip.reference_no,
			si.customer.as_("party"),
			si.customer_name.as_("party_name"),
			si.posting_date,
			si.currency,
			si.outstanding_amount.as_("outstanding"),
		)
		.where(si.docstatus == 1)
		.where(sip.clearance_date.isnull())
		.where(sip.account == gl_account)
		.where(sip.amount > 0.0)
		.where(si.currency == currency)
		.orderby(si.posting_date, order=Order.desc)
		.limit(MAX_ROWS_PER_QUERY)
	).run(as_dict=True)


def _get_purchase_invoice_rows(gl_account: str, currency: str) -> list[dict]:
	# mirrors get_pi_matching_query (withdrawals only): is_paid invoices with
	# clearance_date IS NULL paid from the bank GL account, currency match
	pi = frappe.qb.DocType("Purchase Invoice")
	return (
		frappe.qb.from_(pi)
		.select(
			ConstantColumn("Purchase Invoice").as_("doctype"),
			pi.name,
			pi.paid_amount,
			pi.bill_no,
			pi.supplier.as_("party"),
			pi.supplier_name.as_("party_name"),
			pi.posting_date,
			pi.currency,
			pi.outstanding_amount.as_("outstanding"),
		)
		.where(pi.docstatus == 1)
		.where(pi.is_paid == 1)
		.where(pi.clearance_date.isnull())
		.where(pi.cash_bank_account == gl_account)
		.where(pi.paid_amount > 0.0)
		.where(pi.currency == currency)
		.orderby(pi.posting_date, order=Order.desc)
		.limit(MAX_ROWS_PER_QUERY)
	).run(as_dict=True)


# ---------------------------------------------------------------------------
# Match history (learning signal from previously reconciled transactions)
# ---------------------------------------------------------------------------


def get_match_history(bank_account: str, lookback_days: int = 365) -> MatchHistory:
	"""Aggregate prior reconciliations on a bank account into a MatchHistory.

	Reads the ``payment_entries`` child table (Bank Transaction Payments:
	``payment_document``, ``payment_entry``, ``allocated_amount``) of
	submitted, **Reconciled** Bank Transactions within the lookback window,
	resolves each voucher's party, and counts (party, voucher_type) pairs
	plus dominant party per normalized description prefix.
	"""
	from_date = add_days(getdate(), -lookback_days)

	bt = frappe.qb.DocType("Bank Transaction")
	btp = frappe.qb.DocType("Bank Transaction Payments")
	rows = (
		frappe.qb.from_(btp)
		.join(bt)
		.on(btp.parent == bt.name)
		.select(
			bt.description,
			btp.payment_document,
			btp.payment_entry,
		)
		.where(bt.docstatus == 1)
		.where(bt.status == "Reconciled")
		.where(bt.bank_account == bank_account)
		.where(bt.date >= from_date)
	).run(as_dict=True)

	party_by_voucher = _resolve_voucher_parties(rows)

	party_voucher_counts: dict[tuple[str, str], int] = {}
	prefix_party_counts: dict[str, dict[str, int]] = {}

	for row in rows:
		party = party_by_voucher.get((row.payment_document, row.payment_entry))
		if not party:
			continue

		key = (party, row.payment_document)
		party_voucher_counts[key] = party_voucher_counts.get(key, 0) + 1

		prefix = description_prefix(row.description)
		if prefix:
			bucket = prefix_party_counts.setdefault(prefix, {})
			bucket[party] = bucket.get(party, 0) + 1

	description_party = {}
	for prefix, bucket in prefix_party_counts.items():
		# dominant party for this statement prefix (deterministic tie-break)
		party, count = sorted(bucket.items(), key=lambda item: (-item[1], item[0]))[0]
		description_party[prefix] = (party, count)

	return MatchHistory(
		party_voucher_counts=party_voucher_counts,
		description_party=description_party,
	)


def _resolve_voucher_parties(rows) -> dict[tuple[str, str], str]:
	"""Map (payment_document, payment_entry) -> party for history rows."""
	names_by_doctype: dict[str, set[str]] = {}
	for row in rows:
		names_by_doctype.setdefault(row.payment_document, set()).add(row.payment_entry)

	party_field = {
		"Payment Entry": "party",
		"Journal Entry": "pay_to_recd_from",
		"Sales Invoice": "customer",
		"Purchase Invoice": "supplier",
	}

	resolved: dict[tuple[str, str], str] = {}
	for doctype, names in names_by_doctype.items():
		fieldname = party_field.get(doctype)
		if not fieldname:
			continue
		for voucher in frappe.get_all(
			doctype, filters={"name": ("in", list(names))}, fields=["name", fieldname]
		):
			party = voucher.get(fieldname)
			if party:
				resolved[(doctype, voucher.name)] = party

	return resolved
