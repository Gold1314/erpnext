# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Inbound E-Invoice Queue: the AP clerk's worklist.

One row per staged supplier e-invoice with what is blocking it — how much of
it matched a Purchase Order, the first exceptions, and how long it has been
sitting there. Ageing is the number the queue exists for: an invoice stuck in
*Exception* for three weeks is a payment-terms problem, not a data problem.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, today

STATUS_ORDER = ["Pending Review", "Matched", "Exception", "Invoice Created", "Rejected"]
OPEN_STATUSES = ("Pending Review", "Matched", "Exception")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	data = get_data(filters)
	return get_columns(), data, None, get_chart(data), get_report_summary(data)


def get_columns():
	return [
		{
			"label": _("E-Invoice"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Inbound E-Invoice",
			"width": 140,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Supplier"),
			"fieldname": "supplier",
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 170,
		},
		{
			"label": _("Seller (as sent)"),
			"fieldname": "supplier_name_parsed",
			"fieldtype": "Data",
			"width": 170,
		},
		{
			"label": _("Supplier Invoice No"),
			"fieldname": "invoice_id",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("Issue Date"),
			"fieldname": "issue_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Due Date"),
			"fieldname": "due_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Grand Total"),
			"fieldname": "grand_total",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"width": 80,
			"hidden": 1,
		},
		{
			"label": _("Matched %"),
			"fieldname": "matched_pct",
			"fieldtype": "Percent",
			"width": 100,
		},
		{
			"label": _("Lines"),
			"fieldname": "line_count",
			"fieldtype": "Int",
			"width": 70,
		},
		{
			"label": _("Ageing (Days)"),
			"fieldname": "ageing_days",
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"label": _("Purchase Invoice"),
			"fieldname": "purchase_invoice",
			"fieldtype": "Link",
			"options": "Purchase Invoice",
			"width": 150,
		},
		{
			"label": _("Exceptions"),
			"fieldname": "exceptions_summary",
			"fieldtype": "Data",
			"width": 320,
		},
	]


def _get_match_stats(names: list[str]) -> dict[str, dict]:
	"""Matched-line counts per parent, in one query."""
	stats: dict[str, dict] = {name: {"lines": 0, "matched": 0} for name in names}
	if not names:
		return stats

	for row in frappe.get_all(
		"Inbound E-Invoice Item",
		filters={"parent": ("in", names), "parenttype": "Inbound E-Invoice"},
		fields=["parent", "matched_po_detail", "within_tolerance"],
	):
		entry = stats.setdefault(row.parent, {"lines": 0, "matched": 0})
		entry["lines"] += 1
		if row.matched_po_detail and row.within_tolerance:
			entry["matched"] += 1

	return stats


def get_data(filters):
	conditions = {}
	if filters.get("company"):
		conditions["company"] = filters.company
	if filters.get("supplier"):
		conditions["supplier"] = filters.supplier
	if filters.get("status"):
		conditions["status"] = filters.status
	if filters.get("from_date") and filters.get("to_date"):
		conditions["issue_date"] = ("between", [filters.from_date, filters.to_date])
	elif filters.get("from_date"):
		conditions["issue_date"] = (">=", filters.from_date)
	elif filters.get("to_date"):
		conditions["issue_date"] = ("<=", filters.to_date)

	rows = frappe.get_all(
		"Inbound E-Invoice",
		filters=conditions,
		fields=[
			"name",
			"status",
			"supplier",
			"supplier_name_parsed",
			"invoice_id",
			"issue_date",
			"due_date",
			"currency",
			"grand_total",
			"purchase_invoice",
			"exceptions",
			"creation",
		],
		order_by="creation desc",
	)

	stats = _get_match_stats([row.name for row in rows])
	as_of = today()
	data = []

	for row in rows:
		if filters.get("only_exceptions") and not row.exceptions:
			continue

		entry = stats.get(row.name, {"lines": 0, "matched": 0})
		row.line_count = entry["lines"]
		row.matched_pct = flt(entry["matched"] * 100.0 / entry["lines"], 2) if entry["lines"] else 0.0
		# ageing runs from receipt (creation), not the invoice date: it
		# measures how long *we* have been sitting on it
		row.ageing_days = date_diff(as_of, row.creation)
		exceptions = [line for line in (row.exceptions or "").split("\n") if line.strip()]
		row.exceptions_summary = "; ".join(exceptions[:2]) + (
			_(" (+{0} more)").format(len(exceptions) - 2) if len(exceptions) > 2 else ""
		)
		data.append(row)

	return data


def get_chart(data):
	counts = dict.fromkeys(STATUS_ORDER, 0)
	for row in data:
		counts[row.status] = counts.get(row.status, 0) + 1

	return {
		"data": {
			"labels": [_(status) for status in counts],
			"datasets": [{"name": _("E-Invoices"), "values": list(counts.values())}],
		},
		"type": "bar",
		"colors": ["#ecad4b", "#5e64ff", "#ff5858", "#28a745", "#adb5bd"],
	}


def get_report_summary(data):
	pending = sum(1 for row in data if row.status == "Pending Review")
	exceptions = sum(1 for row in data if row.status == "Exception")
	open_rows = [row for row in data if row.status in OPEN_STATUSES]
	auto_matched = flt(sum(row.matched_pct for row in open_rows) / len(open_rows), 1) if open_rows else 0.0
	oldest = max((row.ageing_days for row in open_rows), default=0)

	return [
		{
			"label": _("Pending Review"),
			"value": pending,
			"indicator": "Orange" if pending else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Exceptions"),
			"value": exceptions,
			"indicator": "Red" if exceptions else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Auto-Matched"),
			"value": auto_matched,
			"indicator": "Green" if auto_matched >= 80 else "Orange",
			"datatype": "Percent",
		},
		{
			"label": _("Oldest Open (Days)"),
			"value": oldest,
			"indicator": "Red" if oldest > 14 else "Blue",
			"datatype": "Int",
		},
	]
