# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_months, flt, getdate, nowdate


def execute(filters: dict | None = None):
	"""One row per submitted Lease Contract, plus the ASC 842 maturity chart."""
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data, maturity_totals = get_data(filters)
	chart = get_chart(maturity_totals)

	return columns, data, None, chart


def get_columns() -> list[dict]:
	return [
		{
			"label": _("Lease Contract"),
			"fieldname": "lease_contract",
			"fieldtype": "Link",
			"options": "Lease Contract",
			"width": 140,
		},
		{
			"label": _("Lease Name"),
			"fieldname": "lease_name",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("Lessor"),
			"fieldname": "lessor",
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 140,
		},
		{
			"label": _("Commencement"),
			"fieldname": "commencement_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("End Date"),
			"fieldname": "end_date",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Short Term"),
			"fieldname": "is_short_term",
			"fieldtype": "Check",
			"width": 90,
		},
		{
			"label": _("Initial Liability"),
			"fieldname": "initial_liability",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Posted Through"),
			"fieldname": "posted_through",
			"fieldtype": "Date",
			"width": 110,
		},
		{
			"label": _("Current Liability"),
			"fieldname": "current_liability",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Remaining Interest"),
			"fieldname": "remaining_interest",
			"fieldtype": "Currency",
			"width": 130,
		},
	]


def get_data(filters) -> tuple[list[dict], dict]:
	query_filters = {"docstatus": 1}
	for field in ("company", "lessor"):
		if filters.get(field):
			query_filters[field] = filters.get(field)

	contracts = frappe.get_all(
		"Lease Contract",
		filters=query_filters,
		fields=[
			"name",
			"lease_name",
			"lessor",
			"commencement_date",
			"end_date",
			"is_short_term",
			"initial_liability",
			"total_interest",
		],
		order_by="commencement_date",
	)
	if not contracts:
		return [], {}

	schedule_rows = frappe.get_all(
		"Lease Amortization Entry",
		filters={"parent": ("in", [contract.name for contract in contracts]), "parenttype": "Lease Contract"},
		fields=["parent", "period_end", "interest", "principal", "closing_liability", "posted"],
		order_by="parent, period_end",
	)
	rows_by_contract = {}
	for row in schedule_rows:
		rows_by_contract.setdefault(row.parent, []).append(row)

	as_of = getdate(filters.get("as_of_date") or nowdate())
	one_year, five_years = add_months(as_of, 12), add_months(as_of, 60)
	maturity_totals = {"under_1y": 0.0, "1y_to_5y": 0.0, "over_5y": 0.0}
	data = []

	for contract in contracts:
		rows = rows_by_contract.get(contract.name, [])
		posted_rows = [row for row in rows if row.posted]

		current_liability = flt(contract.initial_liability)
		posted_through = None
		if posted_rows:
			posted_through = posted_rows[-1].period_end
			current_liability = flt(posted_rows[-1].closing_liability)

		remaining_interest = flt(contract.total_interest) - flt(
			sum(row.interest for row in posted_rows)
		)

		for row in rows:
			period_end = getdate(row.period_end)
			if period_end <= as_of:
				continue
			principal = flt(row.principal)
			if period_end <= one_year:
				maturity_totals["under_1y"] += principal
			elif period_end <= five_years:
				maturity_totals["1y_to_5y"] += principal
			else:
				maturity_totals["over_5y"] += principal

		data.append(
			{
				"lease_contract": contract.name,
				"lease_name": contract.lease_name,
				"lessor": contract.lessor,
				"commencement_date": contract.commencement_date,
				"end_date": contract.end_date,
				"is_short_term": contract.is_short_term,
				"initial_liability": flt(contract.initial_liability),
				"posted_through": posted_through,
				"current_liability": current_liability,
				"remaining_interest": flt(remaining_interest, 2),
			}
		)

	return data, maturity_totals


def get_chart(maturity_totals: dict) -> dict | None:
	"""ASC 842 disclosure split: undiscounted principal maturing in <1y, 1-5y, >5y."""
	if not maturity_totals:
		return None

	return {
		"data": {
			"labels": [_("Within 1 Year"), _("1 to 5 Years"), _("Beyond 5 Years")],
			"datasets": [
				{
					"name": _("Lease Liability Maturity"),
					"values": [
						flt(maturity_totals.get("under_1y"), 2),
						flt(maturity_totals.get("1y_to_5y"), 2),
						flt(maturity_totals.get("over_5y"), 2),
					],
				}
			],
		},
		"type": "bar",
		"colors": ["#7cd6fd", "#5e64ff", "#743ee2"],
	}
