# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _

CHECK_LABELS = {
	"duplicate_invoice": "Duplicate Invoice",
	"account_outlier": "Account Outlier",
	"rare_combination": "Rare Combination",
	"suspicious_posting": "Suspicious Posting",
	"benford_deviation": "Benford Deviation",
}

SEVERITY_ORDER = ("High", "Medium", "Low")
SEVERITY_COLORS = {"High": "#e03636", "Medium": "#f8814f", "Low": "#7cd6fd"}


def execute(filters: dict | None = None):
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	report_summary = get_report_summary(data)

	return columns, data, None, chart, report_summary


def get_columns() -> list[dict]:
	return [
		{
			"label": _("Finding"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Anomaly Finding",
			"width": 120,
		},
		{
			"label": _("Company"),
			"fieldname": "company",
			"fieldtype": "Link",
			"options": "Company",
			"width": 140,
		},
		{
			"label": _("Check"),
			"fieldname": "check_key",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Severity"),
			"fieldname": "severity",
			"fieldtype": "Data",
			"width": 90,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 110,
		},
		{
			"label": _("Message"),
			"fieldname": "message",
			"fieldtype": "Small Text",
			"width": 360,
		},
		{
			"label": _("Score"),
			"fieldname": "score",
			"fieldtype": "Float",
			"width": 80,
		},
		{
			"label": _("Reference DocType"),
			"fieldname": "reference_doctype",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Reference"),
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 160,
		},
		{
			"label": _("Detected On"),
			"fieldname": "detected_on",
			"fieldtype": "Datetime",
			"width": 160,
		},
	]


def get_data(filters: dict | None = None) -> list[dict]:
	filters = frappe._dict(filters or {})

	query_filters = {}
	for field in ("company", "check_key", "severity", "status"):
		if filters.get(field):
			query_filters[field] = filters.get(field)

	if filters.get("from_date") and filters.get("to_date"):
		query_filters["detected_on"] = ("between", [filters.from_date, filters.to_date])
	elif filters.get("from_date"):
		query_filters["detected_on"] = (">=", filters.from_date)
	elif filters.get("to_date"):
		query_filters["detected_on"] = ("<=", filters.to_date)

	return frappe.get_all(
		"Anomaly Finding",
		filters=query_filters,
		fields=[
			"name",
			"company",
			"check_key",
			"severity",
			"status",
			"message",
			"score",
			"reference_doctype",
			"reference_name",
			"detected_on",
		],
		order_by="detected_on desc, name desc",
	)


def get_chart(data: list[dict]) -> dict:
	"""Stacked bar: findings per check, split by severity."""
	check_keys = [key for key in CHECK_LABELS if any(row.check_key == key for row in data)]
	check_keys += sorted({row.check_key for row in data} - set(check_keys))

	datasets = []
	for severity in SEVERITY_ORDER:
		datasets.append(
			{
				"name": _(severity),
				"values": [
					sum(1 for row in data if row.check_key == key and row.severity == severity)
					for key in check_keys
				],
			}
		)

	return {
		"data": {
			"labels": [_(CHECK_LABELS.get(key, key)) for key in check_keys],
			"datasets": datasets,
		},
		"type": "bar",
		"barOptions": {"stacked": 1},
		"colors": [SEVERITY_COLORS[s] for s in SEVERITY_ORDER],
	}


def get_report_summary(data: list[dict]) -> list[dict]:
	open_statuses = ("Open", "Investigating", "Confirmed Issue")
	open_rows = [row for row in data if row.status in open_statuses]
	open_high = sum(1 for row in open_rows if row.severity == "High")
	false_positives = sum(1 for row in data if row.status == "False Positive")
	fp_rate = (100.0 * false_positives / len(data)) if data else 0.0

	return [
		{
			"value": open_high,
			"label": _("Open High Severity"),
			"datatype": "Int",
			"indicator": "Red" if open_high else "Green",
		},
		{
			"value": len(open_rows),
			"label": _("Open Findings"),
			"datatype": "Int",
			"indicator": "Orange" if open_rows else "Green",
		},
		{
			"value": round(fp_rate, 1),
			"label": _("False Positive Rate (%)"),
			"datatype": "Percent",
			"indicator": "Blue",
		},
	]
