# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted API for the reconciliation copilot.

Consumed by the /banking SPA (or any client). Read-only except for
``accept_suggestion``, which delegates the actual write to the existing
reconciliation tool's ``reconcile_vouchers``.

There is deliberately **no** endpoint that auto-matches without a human:
``auto_match_eligible`` is a UI flag only. A one-click accept is still a
click.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.accounts.recon_copilot import loaders
from erpnext.accounts.recon_copilot.engine import explain_weights, score_candidates
from erpnext.accounts.recon_copilot.loaders import VOUCHER_TYPES
from erpnext.accounts.recon_copilot.models import ScoredSuggestion

#: caps for suggest_bulk - keeps a single request bounded (heavier runs
#: should go through the report or a background job)
MAX_BULK_TRANSACTIONS = 200
MAX_SUGGESTIONS_PER_TXN = 20


@frappe.whitelist(methods=["GET"])
def suggest(bank_transaction: str, limit: int = 10) -> list[dict]:
	"""Ranked reconciliation suggestions for one Bank Transaction (read-only)."""
	frappe.has_permission("Bank Transaction", "read", bank_transaction, throw=True)

	limit = min(max(cint(limit) or 10, 1), MAX_SUGGESTIONS_PER_TXN)

	txn = frappe.db.get_value("Bank Transaction", bank_transaction, ["name", "bank_account"], as_dict=True)
	if not txn:
		frappe.throw(_("Bank Transaction {0} not found").format(bank_transaction))

	features = loaders.get_transaction_features(bank_transaction)
	candidates = loaders.get_candidates(bank_transaction)
	history = loaders.get_match_history(txn.bank_account)

	suggestions = score_candidates(features, candidates, history=history, today=frappe.utils.getdate())
	return [_serialize(suggestion) for suggestion in suggestions[:limit]]


@frappe.whitelist(methods=["GET"])
def suggest_bulk(
	bank_account: str,
	from_date: str | None = None,
	to_date: str | None = None,
	limit_per_txn: int = 5,
	max_txns: int = 200,
) -> dict:
	"""Suggestions for the unreconciled transactions of a bank account.

	Returns ``{"transactions": {txn_name: [suggestions...]}, "capped": bool,
	"total_unreconciled": int}``. Work is capped at ``MAX_BULK_TRANSACTIONS``
	transactions (oldest first, mirroring ``get_bank_transactions``'s
	``order_by="date"``) and ``MAX_SUGGESTIONS_PER_TXN`` suggestions each.
	"""
	frappe.has_permission("Bank Account", "read", bank_account, throw=True)
	frappe.has_permission("Bank Transaction", "read", throw=True)

	limit_per_txn = min(max(cint(limit_per_txn) or 5, 1), MAX_SUGGESTIONS_PER_TXN)
	max_txns = min(max(cint(max_txns) or MAX_BULK_TRANSACTIONS, 1), MAX_BULK_TRANSACTIONS)

	# same unreconciled-transaction filters as
	# bank_reconciliation_tool.get_bank_transactions
	filters = [
		["bank_account", "=", bank_account],
		["docstatus", "=", 1],
		["unallocated_amount", ">", 0.0],
	]
	if from_date:
		filters.append(["date", ">=", from_date])
	if to_date:
		filters.append(["date", "<=", to_date])

	total_unreconciled = frappe.db.count("Bank Transaction", filters=filters)
	transactions = frappe.get_all(
		"Bank Transaction",
		filters=filters,
		fields=["name", "deposit", "withdrawal", "bank_account"],
		order_by="date asc, name asc",
		limit_page_length=max_txns,
	)

	history = loaders.get_match_history(bank_account)
	today = frappe.utils.getdate()

	result = {}
	for transaction in transactions:
		features = loaders.get_transaction_features(transaction.name)
		candidates = loaders.get_candidates(transaction)
		suggestions = score_candidates(features, candidates, history=history, today=today)
		result[transaction.name] = [_serialize(suggestion) for suggestion in suggestions[:limit_per_txn]]

	return {
		"transactions": result,
		"capped": total_unreconciled > len(transactions),
		"total_unreconciled": total_unreconciled,
	}


@frappe.whitelist(methods=["POST"])
def accept_suggestion(bank_transaction: str, voucher_type: str, voucher_name: str) -> dict:
	"""Reconcile ``voucher_name`` against ``bank_transaction``.

	Delegates the write to the existing tool's whitelisted
	``bank_reconciliation_tool.reconcile_vouchers`` (which runs the Bank
	Transaction's own ``add_payment_entries`` / ``allocate_payment_entries``
	pipeline), passing the voucher's remaining amount exactly like
	``start_auto_reconcile`` does after ``subtract_allocations``.
	"""
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		reconcile_vouchers,
	)

	frappe.has_permission("Bank Transaction", "write", bank_transaction, throw=True)

	if voucher_type not in VOUCHER_TYPES:
		frappe.throw(_("Voucher type {0} is not supported for reconciliation").format(voucher_type))

	# re-derive the candidate from the same mirrored queries; this both
	# validates that the voucher is still unreconciled-side and yields its
	# allocation-adjusted remaining amount
	candidate = next(
		(
			row
			for row in loaders.get_candidates(bank_transaction)
			if row.voucher_type == voucher_type and row.voucher_name == voucher_name
		),
		None,
	)
	if candidate is None:
		frappe.throw(
			_("{0} {1} is not an open reconciliation candidate for this transaction").format(
				voucher_type, voucher_name
			)
		)

	vouchers = json.dumps(
		[
			{
				"payment_doctype": candidate.voucher_type,
				"payment_name": candidate.voucher_name,
				"amount": flt(candidate.amount),
			}
		]
	)
	transaction = reconcile_vouchers(bank_transaction, vouchers)

	return {
		"bank_transaction": transaction.name,
		"status": transaction.status,
		"allocated_amount": transaction.allocated_amount,
		"unallocated_amount": transaction.unallocated_amount,
	}


@frappe.whitelist(methods=["GET"])
def get_score_weights() -> list[dict]:
	"""Weight breakdown for the UI legend (engine.explain_weights)."""
	return explain_weights()


def _serialize(suggestion: ScoredSuggestion) -> dict:
	candidate = suggestion.candidate
	return {
		"voucher_type": candidate.voucher_type,
		"voucher_name": candidate.voucher_name,
		"amount": candidate.amount,
		"date": candidate.date.isoformat() if candidate.date else None,
		"party": candidate.party,
		"party_name": candidate.party_name,
		"reference_no": candidate.reference_no,
		"bill_no": candidate.bill_no,
		"currency": candidate.currency,
		"outstanding": candidate.outstanding,
		"score": suggestion.score,
		"features": suggestion.features,
		"explanation": suggestion.explanation,
		"auto_match_eligible": suggestion.auto_match_eligible,
	}
