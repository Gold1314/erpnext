# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Reconciliation Suggestions script report.

For each unreconciled Bank Transaction of a bank account (optional date
range), shows the copilot's top-ranked voucher with score, explanation and a
status bucket:

- Strong  score >= 95
- Good    80 <= score < 95
- Weak    0 < score < 80 (or a scored candidate below 80)
- None    no candidate at all

Work is capped at MAX_TRANSACTIONS unreconciled transactions (oldest first,
mirroring how ``bank_reconciliation_tool.get_bank_transactions`` orders by
date and how ``auto_reconcile_vouchers`` batches heavy runs); a message is
shown when the cap truncates the list.
"""

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.recon_copilot import loaders
from erpnext.accounts.recon_copilot.engine import score_candidates

MAX_TRANSACTIONS = 200

STRONG_THRESHOLD = 95.0
GOOD_THRESHOLD = 80.0

BUCKETS = ("Strong", "Good", "Weak", "None")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.bank_account:
		frappe.throw(_("Bank Account filter is required"))

	frappe.has_permission("Bank Account", "read", filters.bank_account, throw=True)

	data, capped, currency = get_data(filters)
	message = None
	if capped:
		message = _(
			"Showing the oldest {0} unreconciled transactions. Narrow the date range to see the rest."
		).format(MAX_TRANSACTIONS)

	return get_columns(), data, message, get_chart(data), get_summary(data, currency)


def get_data(filters):
	transaction_filters = [
		["bank_account", "=", filters.bank_account],
		["docstatus", "=", 1],
		["unallocated_amount", ">", 0.0],
	]
	if filters.from_date:
		transaction_filters.append(["date", ">=", filters.from_date])
	if filters.to_date:
		transaction_filters.append(["date", "<=", filters.to_date])

	total = frappe.db.count("Bank Transaction", filters=transaction_filters)
	transactions = frappe.get_all(
		"Bank Transaction",
		filters=transaction_filters,
		fields=[
			"name",
			"date",
			"deposit",
			"withdrawal",
			"currency",
			"description",
			"unallocated_amount",
			"party",
			"bank_account",
		],
		order_by="date asc, name asc",
		limit_page_length=MAX_TRANSACTIONS,
	)

	history = loaders.get_match_history(filters.bank_account)
	today = frappe.utils.getdate()
	currency = transactions[0].currency if transactions else None

	data = []
	for transaction in transactions:
		features = loaders.get_transaction_features(transaction.name)
		candidates = loaders.get_candidates(transaction)
		suggestions = score_candidates(features, candidates, history=history, today=today)

		row = {
			"date": transaction.date,
			"bank_transaction": transaction.name,
			"description": (transaction.description or "")[:140],
			"unallocated_amount": flt(transaction.unallocated_amount),
			"currency": transaction.currency,
			"party": transaction.party,
			"voucher_type": None,
			"voucher": None,
			"score": None,
			"match_bucket": "None",
			"explanation": None,
		}

		if suggestions:
			top = suggestions[0]
			row.update(
				{
					"voucher_type": top.candidate.voucher_type,
					"voucher": top.candidate.voucher_name,
					"score": top.score,
					"match_bucket": get_bucket(top.score),
					"explanation": top.explanation,
				}
			)

		data.append(row)

	return data, total > len(transactions), currency


def get_bucket(score):
	if score >= STRONG_THRESHOLD:
		return "Strong"
	if score >= GOOD_THRESHOLD:
		return "Good"
	return "Weak"


def get_columns():
	return [
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 100},
		{
			"label": _("Bank Transaction"),
			"fieldname": "bank_transaction",
			"fieldtype": "Link",
			"options": "Bank Transaction",
			"width": 160,
		},
		{"label": _("Description"), "fieldname": "description", "fieldtype": "Data", "width": 220},
		{
			"label": _("Unallocated Amount"),
			"fieldname": "unallocated_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 140,
		},
		{"label": _("Party"), "fieldname": "party", "fieldtype": "Data", "width": 130},
		{
			"label": _("Suggested Voucher Type"),
			"fieldname": "voucher_type",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Suggested Voucher"),
			"fieldname": "voucher",
			"fieldtype": "Dynamic Link",
			"options": "voucher_type",
			"width": 160,
		},
		{"label": _("Score"), "fieldname": "score", "fieldtype": "Float", "precision": 2, "width": 80},
		{"label": _("Match"), "fieldname": "match_bucket", "fieldtype": "Data", "width": 90},
		{"label": _("Why"), "fieldname": "explanation", "fieldtype": "Data", "width": 320},
	]


def get_chart(data):
	counts = dict.fromkeys(BUCKETS, 0)
	for row in data:
		counts[row["match_bucket"]] += 1

	return {
		"data": {
			"labels": [_(bucket) for bucket in BUCKETS],
			"datasets": [{"name": _("Transactions"), "values": [counts[bucket] for bucket in BUCKETS]}],
		},
		"type": "bar",
		"colors": ["#28a745", "#5e64ff", "#ffa00a", "#ff5858"],
	}


def get_summary(data, currency):
	strong_count = sum(1 for row in data if row["match_bucket"] == "Strong")
	total_unallocated = sum(row["unallocated_amount"] for row in data)

	return [
		{
			"value": len(data),
			"label": _("Unreconciled Transactions"),
			"datatype": "Int",
		},
		{
			"value": strong_count,
			"label": _("Strong Matches"),
			"indicator": "Green" if strong_count else "Red",
			"datatype": "Int",
		},
		{
			"value": total_unallocated,
			"label": _("Total Unallocated Amount"),
			"datatype": "Currency",
			"currency": currency,
		},
	]
