# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the multi-GAAP adjustment engine.

All DB access lives here; ``engine.py`` stays pure. Amounts are in company
currency (`tabGL Entry`.debit / .credit are company-currency fields).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext.accounts.multibook.models import PolicyRule


def get_policy_rules(company: str, finance_book: str) -> list[PolicyRule]:
	"""Enabled ``Accounting Policy Rule`` rows for one company + finance book,
	as pure dataclasses (deterministic order: rule name)."""
	rows = frappe.get_all(
		"Accounting Policy Rule",
		filters={"company": company, "finance_book": finance_book, "enabled": 1},
		fields=[
			"name",
			"rule_type",
			"source_account",
			"target_account",
			"percentage",
			"manual_amount",
			"manual_debit_account",
			"manual_credit_account",
			"description",
		],
		order_by="name",
	)
	return [
		PolicyRule(
			name=row.name,
			rule_type=row.rule_type,
			source_account=row.source_account,
			target_account=row.target_account,
			percentage=flt(row.percentage) or 100.0,
			manual_amount=flt(row.manual_amount),
			manual_debit_account=row.manual_debit_account,
			manual_credit_account=row.manual_credit_account,
			description=row.description or "",
		)
		for row in rows
	]


def get_account_movements(company: str, accounts: list[str], from_date, to_date) -> dict[str, float]:
	"""Net movement (``SUM(debit) - SUM(credit)``, positive = net debit) per
	account over the period, **from the common (untagged) GL layer only**:
	``finance_book = '' OR finance_book IS NULL``.

	Why untagged-only (re-run / compounding protection): in the book-B report
	view (``finance_book IN (B, '') OR finance_book IS NULL``, quoted in
	DESIGN.md) the base numbers every book shares are exactly the untagged
	entries. Adjustment JEs are tagged with the finance book, so they never
	feed back into this base - recomputing or re-running a period cannot
	compound earlier adjustments. Entries tagged with *other* books are
	equally invisible in book B's view and are excluded too.

	Cancelled ledger rows are excluded via ``is_cancelled = 0`` (same
	condition the General Ledger report and ``get_balance_on`` use; GL Entry
	is submittable but reports filter on ``is_cancelled``, not docstatus).
	"""
	if not accounts:
		return {}

	rows = frappe.db.sql(
		"""
		SELECT account, SUM(debit) - SUM(credit) AS net_movement
		FROM `tabGL Entry`
		WHERE company = %(company)s
			AND account IN %(accounts)s
			AND posting_date >= %(from_date)s
			AND posting_date <= %(to_date)s
			AND is_cancelled = 0
			AND (finance_book = '' OR finance_book IS NULL)
		GROUP BY account
		""",
		{
			"company": company,
			"accounts": tuple(accounts),
			"from_date": from_date,
			"to_date": to_date,
		},
		as_dict=True,
	)
	return {row.account: flt(row.net_movement) for row in rows}
