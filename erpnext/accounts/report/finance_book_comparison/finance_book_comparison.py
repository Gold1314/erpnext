# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Finance Book Comparison - per-account balances of two finance-book views.

A "book B view" mirrors the General Ledger report's finance-book filter
exactly (erpnext/accounts/report/general_ledger/general_ledger.py:327):

	(finance_book in (%(finance_book)s, '') OR finance_book IS NULL)

i.e. entries tagged with book B **plus** all untagged (common) entries. The
"default book view" (finance_book_2 left empty) mirrors line 329:

	(finance_book in ('') OR finance_book IS NULL)

i.e. untagged entries only. See erpnext/accounts/multibook/DESIGN.md for the
full quoted semantics.

Balance semantics per account (all company currency, is_cancelled = 0):

- Income / Expense: net movement inside From Date .. To Date;
- Asset / Liability / Equity: closing balance as of To Date (opening before
  From Date plus period movement - so From Date only affects P&L rows);
- balances are shown with their natural sign (debit-normal for Asset/Expense,
  credit-normal for Liability/Equity/Income); Delta = Book 1 - Book 2.
"""

import frappe
from frappe import _
from frappe.utils import flt

ROOT_TYPES = ("Asset", "Liability", "Equity", "Income", "Expense")
CREDIT_NORMAL_ROOT_TYPES = ("Liability", "Equity", "Income")
BALANCE_SHEET_ROOT_TYPES = ("Asset", "Liability", "Equity")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	validate_filters(filters)

	columns = get_columns(filters)
	data = get_data(filters)
	chart = get_chart(filters, data)
	return columns, data, None, chart


def validate_filters(filters):
	for fieldname, label in (
		("company", _("Company")),
		("to_date", _("To Date")),
		("finance_book_1", _("Finance Book 1")),
	):
		if not filters.get(fieldname):
			frappe.throw(_("{0} is a mandatory filter.").format(label))

	if filters.get("from_date") and filters.from_date > filters.to_date:
		frappe.throw(_("From Date cannot be after To Date."))

	if filters.get("finance_book_2") and filters.finance_book_2 == filters.finance_book_1:
		frappe.throw(_("Pick two different finance books (leave Finance Book 2 empty for the default book view)."))


def get_columns(filters):
	book_2_label = filters.get("finance_book_2") or _("Default Book")
	return [
		{
			"fieldname": "account",
			"label": _("Account"),
			"fieldtype": "Link",
			"options": "Account",
			"width": 300,
		},
		{
			"fieldname": "root_type",
			"label": _("Root Type"),
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"fieldname": "book_1_balance",
			"label": _("{0} (Book 1)").format(filters.finance_book_1),
			"fieldtype": "Currency",
			"width": 180,
		},
		{
			"fieldname": "book_2_balance",
			"label": _("{0} (Book 2)").format(book_2_label),
			"fieldtype": "Currency",
			"width": 180,
		},
		{
			"fieldname": "delta",
			"label": _("Delta (Book 1 - Book 2)"),
			"fieldtype": "Currency",
			"width": 180,
		},
	]


def get_book_condition(book_param: str | None) -> str:
	"""The exact finance-book view predicate of the General Ledger report.

	``book_param`` is the name of a query parameter holding a finance book,
	or ``None`` for the default-book view (untagged entries only).
	"""
	if book_param:
		return f"(gle.finance_book IN (%({book_param})s, '') OR gle.finance_book IS NULL)"
	return "(gle.finance_book IN ('') OR gle.finance_book IS NULL)"


def get_raw_balances(filters) -> list[dict]:
	"""Per account: period and pre-period sums of (debit - credit) per book view."""
	book_1 = get_book_condition("finance_book_1")
	book_2 = get_book_condition("finance_book_2" if filters.get("finance_book_2") else None)

	if filters.get("from_date"):
		in_period = "gle.posting_date >= %(from_date)s"
	else:
		in_period = "1 = 1"

	return frappe.db.sql(
		f"""
		SELECT
			gle.account,
			SUM(CASE WHEN {book_1} AND {in_period} THEN gle.debit - gle.credit ELSE 0 END) AS book_1_period,
			SUM(CASE WHEN {book_1} AND NOT ({in_period}) THEN gle.debit - gle.credit ELSE 0 END) AS book_1_before,
			SUM(CASE WHEN {book_2} AND {in_period} THEN gle.debit - gle.credit ELSE 0 END) AS book_2_period,
			SUM(CASE WHEN {book_2} AND NOT ({in_period}) THEN gle.debit - gle.credit ELSE 0 END) AS book_2_before
		FROM `tabGL Entry` gle
		WHERE gle.company = %(company)s
			AND gle.is_cancelled = 0
			AND gle.posting_date <= %(to_date)s
		GROUP BY gle.account
		""",
		filters,
		as_dict=True,
	)


def get_data(filters):
	accounts = {
		account.name: account
		for account in frappe.get_all(
			"Account",
			filters={"company": filters.company, "is_group": 0},
			fields=["name", "account_name", "root_type"],
		)
	}

	rows_by_root: dict[str, list[dict]] = {root_type: [] for root_type in ROOT_TYPES}
	for raw in get_raw_balances(filters):
		account = accounts.get(raw.account)
		if not account or account.root_type not in rows_by_root:
			continue
		if filters.get("root_type") and account.root_type != filters.root_type:
			continue

		sign = -1 if account.root_type in CREDIT_NORMAL_ROOT_TYPES else 1
		include_opening = account.root_type in BALANCE_SHEET_ROOT_TYPES
		book_1 = flt(raw.book_1_period) + (flt(raw.book_1_before) if include_opening else 0)
		book_2 = flt(raw.book_2_period) + (flt(raw.book_2_before) if include_opening else 0)
		book_1, book_2 = flt(sign * book_1, 2), flt(sign * book_2, 2)

		if not (book_1 or book_2):
			continue

		rows_by_root[account.root_type].append(
			{
				"account": raw.account,
				"root_type": account.root_type,
				"book_1_balance": book_1,
				"book_2_balance": book_2,
				"delta": flt(book_1 - book_2, 2),
				"indent": 1,
			}
		)

	data = []
	for root_type in ROOT_TYPES:
		rows = rows_by_root[root_type]
		if not rows:
			continue
		rows.sort(key=lambda row: -abs(row["delta"]))
		data.append(
			{
				"account": _(root_type),
				"root_type": root_type,
				"book_1_balance": flt(sum(row["book_1_balance"] for row in rows), 2),
				"book_2_balance": flt(sum(row["book_2_balance"] for row in rows), 2),
				"delta": flt(sum(row["delta"] for row in rows), 2),
				"indent": 0,
				"is_group_row": 1,
			}
		)
		data.extend(rows)

	return data


def get_chart(filters, data):
	movers = sorted(
		(row for row in data if not row.get("is_group_row") and row["delta"]),
		key=lambda row: -abs(row["delta"]),
	)[:10]
	if not movers:
		return None

	return {
		"type": "bar",
		"data": {
			"labels": [row["account"] for row in movers],
			"datasets": [
				{
					"name": _("Delta (Book 1 - Book 2)"),
					"values": [row["delta"] for row in movers],
				}
			],
		},
		"colors": ["#7cd6fd"],
		"title": _("Top 10 Deltas by Account"),
	}
