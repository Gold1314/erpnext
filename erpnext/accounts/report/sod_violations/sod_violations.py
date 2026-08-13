# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def execute(filters: dict | None = None):
	"""Return columns, data and a risk-level summary chart for the report."""
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)

	return columns, data, None, chart


def get_columns() -> list[dict]:
	return [
		{
			"label": _("User"),
			"fieldname": "user",
			"fieldtype": "Link",
			"options": "User",
			"width": 200,
		},
		{
			"label": _("Full Name"),
			"fieldname": "full_name",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("SoD Rule"),
			"fieldname": "sod_rule",
			"fieldtype": "Link",
			"options": "SoD Rule",
			"width": 240,
		},
		{
			"label": _("Risk Level"),
			"fieldname": "risk_level",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("First Function Roles"),
			"fieldname": "first_function_roles",
			"fieldtype": "Small Text",
			"width": 200,
		},
		{
			"label": _("Second Function Roles"),
			"fieldname": "second_function_roles",
			"fieldtype": "Small Text",
			"width": 200,
		},
		{
			"label": _("Detected On"),
			"fieldname": "detected_on",
			"fieldtype": "Datetime",
			"width": 160,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 100,
		},
	]


def get_data(filters: dict | None = None) -> list[dict]:
	query_filters = {}
	if filters:
		for field in ("status", "risk_level", "user"):
			if filters.get(field):
				query_filters[field] = filters.get(field)

	data = frappe.get_all(
		"SoD Violation Log",
		filters=query_filters,
		fields=[
			"user",
			"sod_rule",
			"risk_level",
			"first_function_roles",
			"second_function_roles",
			"detected_on",
			"status",
		],
		order_by="detected_on desc",
	)

	set_full_names(data)
	return data


def set_full_names(data: list[dict]) -> None:
	users = {row.user for row in data if row.user}
	if not users:
		return

	full_names = dict(
		frappe.get_all(
			"User",
			filters={"name": ("in", list(users))},
			fields=["name", "full_name"],
			as_list=True,
		)
	)
	for row in data:
		row["full_name"] = full_names.get(row.user)


def get_chart(data: list[dict]) -> dict:
	counts = {"High": 0, "Medium": 0, "Low": 0}
	for row in data:
		if row.risk_level in counts:
			counts[row.risk_level] += 1

	return {
		"data": {
			"labels": [_("High"), _("Medium"), _("Low")],
			"datasets": [{"name": _("Violations"), "values": list(counts.values())}],
		},
		"type": "donut",
		"colors": ["#e03636", "#f8814f", "#7cd6fd"],
	}
