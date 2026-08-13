# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""AI Extraction Queue: the AP clerk's worklist for LLM-read documents.

One row per staged extraction with what is blocking it — how many fields the
model flagged for review, the first warnings, and how long the document has
been sitting. Structural twin of the EDI ``Inbound E-Invoice Queue`` report:
ageing runs from receipt, because it measures how long *we* sat on it.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, today

STATUS_ORDER = [
	"Pending Extraction",
	"Extracted",
	"Needs Review",
	"Invoice Created",
	"Failed",
	"Rejected",
]
OPEN_STATUSES = ("Pending Extraction", "Extracted", "Needs Review", "Failed")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	data = get_data(filters)
	return get_columns(), data, None, get_chart(data), get_report_summary(data)


def get_columns():
	return [
		{
			"label": _("Document"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "AI Document Extraction",
			"width": 130,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Supplier"),
			"fieldname": "supplier",
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 170,
		},
		{
			"label": _("Supplier (extracted)"),
			"fieldname": "supplier_name_extracted",
			"fieldtype": "Data",
			"width": 170,
		},
		{
			"label": _("Supplier Invoice No"),
			"fieldname": "invoice_number",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": _("Invoice Date"),
			"fieldname": "invoice_date",
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
			"label": _("Flagged Fields"),
			"fieldname": "flagged_fields",
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"label": _("Model"),
			"fieldname": "model_used",
			"fieldtype": "Data",
			"width": 130,
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
			"label": _("Warnings"),
			"fieldname": "warnings_summary",
			"fieldtype": "Data",
			"width": 320,
		},
	]


def get_data(filters):
	conditions = {}
	if filters.get("company"):
		conditions["company"] = filters.company
	if filters.get("supplier"):
		conditions["supplier"] = filters.supplier
	if filters.get("status"):
		conditions["status"] = filters.status
	if filters.get("from_date") and filters.get("to_date"):
		conditions["creation"] = ("between", [filters.from_date, filters.to_date])
	elif filters.get("from_date"):
		conditions["creation"] = (">=", filters.from_date)
	elif filters.get("to_date"):
		conditions["creation"] = ("<=", filters.to_date)

	rows = frappe.get_all(
		"AI Document Extraction",
		filters=conditions,
		fields=[
			"name",
			"status",
			"supplier",
			"supplier_name_extracted",
			"invoice_number",
			"invoice_date",
			"currency",
			"grand_total",
			"needs_review_fields",
			"model_used",
			"purchase_invoice",
			"warnings",
			"creation",
		],
		order_by="creation desc",
	)

	as_of = today()
	data = []
	for row in rows:
		flagged = [field for field in (row.needs_review_fields or "").split("\n") if field.strip()]
		if filters.get("only_flagged") and not flagged:
			continue

		row.flagged_fields = len(flagged)
		# ageing runs from receipt (creation), not the invoice date: it
		# measures how long *we* have been sitting on it
		row.ageing_days = date_diff(as_of, row.creation)
		warnings = [line for line in (row.warnings or "").split("\n") if line.strip()]
		row.warnings_summary = "; ".join(warnings[:2]) + (
			_(" (+{0} more)").format(len(warnings) - 2) if len(warnings) > 2 else ""
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
			"datasets": [{"name": _("Documents"), "values": list(counts.values())}],
		},
		"type": "bar",
		"colors": ["#ecad4b", "#5e64ff", "#f8d200", "#28a745", "#ff5858", "#adb5bd"],
	}


def get_report_summary(data):
	pending = sum(1 for row in data if row.status == "Pending Extraction")
	needs_review = sum(1 for row in data if row.status == "Needs Review")
	created = sum(1 for row in data if row.status == "Invoice Created")
	failed = sum(1 for row in data if row.status == "Failed")
	oldest = max((row.ageing_days for row in data if row.status in OPEN_STATUSES), default=0)

	return [
		{
			"label": _("Pending Extraction"),
			"value": pending,
			"indicator": "Orange" if pending else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Needs Review"),
			"value": needs_review,
			"indicator": "Yellow" if needs_review else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Invoices Created"),
			"value": created,
			"indicator": "Green",
			"datatype": "Int",
		},
		{
			"label": _("Failed"),
			"value": failed,
			"indicator": "Red" if failed else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Oldest Open (Days)"),
			"value": oldest,
			"indicator": "Red" if oldest > 14 else "Blue",
			"datatype": "Int",
		},
	]
