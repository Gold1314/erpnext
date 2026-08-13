# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the anomaly-detection engine.

All DB access lives here; the engine (``engine.py``) stays pure. Field names
below are verified against the doctype JSONs:

- Purchase Invoice: supplier, bill_no, bill_date, grand_total, posting_date
- GL Entry: account, posting_date, debit, credit, cost_center, voucher_type,
  voucher_no, company, is_cancelled
- Journal Entry: name, total_debit, posting_date, company, docstatus and the
  framework-standard ``creation``/``owner`` columns
"""

from __future__ import annotations

import datetime
import re

import frappe
from frappe.utils import add_days, add_months, cint, flt, get_datetime, getdate

from erpnext.accounts.anomaly.models import GLMovement, InvoiceRecord, PostingRecord

#: Anomaly Finding statuses whose fingerprints block re-detection.
#: Resolved is deliberately absent - a resolved anomaly that reappears
#: should be raised again; a False Positive should stay quiet forever.
BLOCKING_STATUSES = ("Open", "Investigating", "Confirmed Issue", "False Positive")

FIELDNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def get_invoices(company: str, days: int = 90) -> list[InvoiceRecord]:
	"""Submitted Purchase Invoices of the last ``days`` days."""
	rows = frappe.get_all(
		"Purchase Invoice",
		filters={
			"company": company,
			"docstatus": 1,
			"posting_date": (">=", add_days(getdate(), -abs(cint(days) or 90))),
		},
		fields=["name", "supplier", "bill_no", "bill_date", "grand_total", "posting_date"],
	)

	return [
		InvoiceRecord(
			name=row.name,
			supplier=row.supplier,
			bill_no=row.bill_no,
			bill_date=getdate(row.bill_date) if row.bill_date else None,
			amount=flt(row.grand_total),
			posting_date=getdate(row.posting_date),
		)
		for row in rows
	]


def get_gl_movements(company: str, months: int = 18) -> dict[str, list[GLMovement]]:
	"""Monthly net movement per account from non-cancelled GL Entries."""
	from_date = add_months(getdate().replace(day=1), -abs(cint(months) or 18))

	rows = frappe.db.sql(
		"""
		select
			gle.account,
			date_format(gle.posting_date, '%%Y-%%m') as period,
			sum(gle.debit) as total_debit,
			sum(gle.credit) as total_credit,
			count(*) as entry_count
		from `tabGL Entry` gle
		where gle.company = %(company)s
			and gle.is_cancelled = 0
			and gle.posting_date >= %(from_date)s
		group by gle.account, period
		""",
		{"company": company, "from_date": from_date},
		as_dict=True,
	)

	movements: dict[str, list[GLMovement]] = {}
	for row in rows:
		movements.setdefault(row.account, []).append(
			GLMovement(
				account=row.account,
				period=row.period,
				total_debit=flt(row.total_debit),
				total_credit=flt(row.total_credit),
				entry_count=cint(row.entry_count),
			)
		)

	return movements


def get_combo_counts(
	company: str, dimension_field: str = "cost_center", months: int = 12
) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
	"""Posting counts per (account, dimension value) and totals per account.

	``dimension_field`` must be an actual column of GL Entry (guarded against
	injection by a fieldname whitelist + meta check).
	"""
	if not FIELDNAME_PATTERN.match(dimension_field) or not frappe.get_meta("GL Entry").has_field(
		dimension_field
	):
		frappe.throw(f"Invalid GL Entry dimension field: {dimension_field}")

	from_date = add_months(getdate(), -abs(cint(months) or 12))

	rows = frappe.db.sql(
		f"""
		select
			gle.account,
			coalesce(gle.`{dimension_field}`, '') as dimension_value,
			count(*) as posting_count
		from `tabGL Entry` gle
		where gle.company = %(company)s
			and gle.is_cancelled = 0
			and gle.posting_date >= %(from_date)s
		group by gle.account, dimension_value
		""",
		{"company": company, "from_date": from_date},
		as_dict=True,
	)

	combo_counts: dict[tuple[str, str], int] = {}
	total_by_account: dict[str, int] = {}
	for row in rows:
		count = cint(row.posting_count)
		combo_counts[(row.account, row.dimension_value)] = count
		total_by_account[row.account] = total_by_account.get(row.account, 0) + count

	return combo_counts, total_by_account


def get_postings(company: str, days: int = 60) -> list[PostingRecord]:
	"""Submitted (manual) Journal Entries of the last ``days`` days.

	Weekday and the backdating delta are derived here in Python (pure date
	arithmetic on the loaded values), never in SQL, so the engine receives
	ready-made, timezone-independent facts.
	"""
	rows = frappe.get_all(
		"Journal Entry",
		filters={
			"company": company,
			"docstatus": 1,
			"posting_date": (">=", add_days(getdate(), -abs(cint(days) or 60))),
		},
		fields=["name", "total_debit", "posting_date", "creation", "owner"],
	)

	postings = []
	for row in rows:
		posting_date = getdate(row.posting_date)
		creation_date = get_datetime(row.creation).date() if row.creation else posting_date
		backdated_days = max(0, (creation_date - posting_date).days)

		postings.append(
			PostingRecord(
				voucher_type="Journal Entry",
				voucher_no=row.name,
				account=None,
				amount=flt(row.total_debit),
				posting_date=posting_date,
				weekday=posting_date.weekday(),
				created_by=row.owner,
				is_backdated_days=backdated_days,
			)
		)

	return postings


def get_amounts_for_benford(company: str, months: int = 12, voucher_type: str | None = None) -> list[float]:
	"""Positive GL debit amounts for first-digit analysis."""
	filters = {
		"company": company,
		"is_cancelled": 0,
		"debit": (">", 0),
		"posting_date": (">=", add_months(getdate(), -abs(cint(months) or 12))),
	}
	if voucher_type:
		filters["voucher_type"] = voucher_type

	return [flt(d) for d in frappe.get_all("GL Entry", filters=filters, pluck="debit")]


def get_known_fingerprints(company: str) -> set[str]:
	"""Fingerprints of findings that must not be raised again (open ones and
	confirmed false positives - see :data:`BLOCKING_STATUSES`)."""
	return set(
		frappe.get_all(
			"Anomaly Finding",
			filters={"company": company, "status": ("in", BLOCKING_STATUSES)},
			pluck="fingerprint",
		)
	)


def get_benford_voucher_types(company: str, months: int = 12) -> list[str]:
	"""Distinct GL voucher types with activity in the window, so the scanner
	can run the Benford screen per voucher type (per-scope, as the engine
	expects)."""
	return frappe.get_all(
		"GL Entry",
		filters={
			"company": company,
			"is_cancelled": 0,
			"posting_date": (">=", add_months(getdate(), -abs(cint(months) or 12))),
		},
		distinct=True,
		pluck="voucher_type",
	)
