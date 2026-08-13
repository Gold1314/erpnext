# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Forecast Accuracy report.

Compares submitted Sales Forecast Item rows whose period has already elapsed
against actual demand loaded from the same sources the statistical generator
uses (Sales Order / Sales Invoice / Delivery Note via
``erpnext.manufacturing.forecasting.loaders``). Detail rows show forecast vs
actual per period with absolute error and APE; a bold summary row per item
carries the item's MAPE and bias.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from erpnext.manufacturing.forecasting import loaders
from erpnext.manufacturing.forecasting.models import next_period_start, period_start


def execute(filters=None):
	filters = frappe._dict(filters or {})
	rows = get_forecast_rows(filters)
	data = build_data(rows, filters)

	return get_columns(), data, None, get_chart_data(data), get_report_summary(data)


def get_forecast_rows(filters):
	forecast = frappe.qb.DocType("Sales Forecast")
	item = frappe.qb.DocType("Sales Forecast Item")

	query = (
		frappe.qb.from_(forecast)
		.inner_join(item)
		.on(forecast.name == item.parent)
		.select(
			item.parent.as_("sales_forecast"),
			forecast.frequency,
			forecast.parent_warehouse,
			item.item_code,
			item.item_name,
			item.delivery_date,
			item.demand_qty.as_("forecast_qty"),
			item.forecast_model,
		)
		.where(
			(forecast.docstatus == 1) & (item.parentfield == "items") & (forecast.company == filters.company)
		)
		.orderby(item.item_code)
		.orderby(item.delivery_date)
	)

	if filters.get("sales_forecast"):
		query = query.where(forecast.name == filters.sales_forecast)

	if filters.get("from_date"):
		query = query.where(item.delivery_date >= filters.from_date)

	if filters.get("to_date"):
		query = query.where(item.delivery_date <= filters.to_date)

	return query.run(as_dict=True)


def build_data(rows, filters):
	today = getdate(nowdate())

	# only periods that have fully elapsed can be scored
	rows = [row for row in rows if next_period_start(row.delivery_date, row.frequency) <= today]
	if not rows:
		return []

	actuals = get_actuals(rows, filters)

	# group scored rows per item for the summary blocks
	item_rows: dict[str, list[dict]] = {}
	for row in rows:
		series = actuals.get((row.frequency, row.parent_warehouse))
		item_series = (series or {}).get(row.item_code)
		actual_qty = item_series.value_at(row.delivery_date) if item_series else 0.0

		forecast_qty = flt(row.forecast_qty)
		error = forecast_qty - actual_qty
		detail = {
			"sales_forecast": row.sales_forecast,
			"item_code": row.item_code,
			"item_name": row.item_name,
			"period": period_start(row.delivery_date, row.frequency),
			"forecast_model": row.forecast_model,
			"forecast_qty": forecast_qty,
			"actual_qty": actual_qty,
			"abs_error": abs(error),
			"error": error,
			"ape": (100.0 * abs(error) / actual_qty) if actual_qty else None,
			"is_summary": 0,
		}
		item_rows.setdefault(row.item_code, []).append(detail)

	data = []
	for item_code in sorted(item_rows):
		details = item_rows[item_code]
		data.extend(details)
		data.append(get_item_summary(item_code, details))

	return data


def get_item_summary(item_code, details):
	apes = [row["ape"] for row in details if row["ape"] is not None]
	total_actual = sum(row["actual_qty"] for row in details)
	total_error = sum(row["error"] for row in details)

	return {
		"item_code": item_code,
		"item_name": _("Summary"),
		"forecast_qty": sum(row["forecast_qty"] for row in details),
		"actual_qty": total_actual,
		"abs_error": sum(row["abs_error"] for row in details),
		"mape": (sum(apes) / len(apes)) if apes else None,
		"bias": (100.0 * total_error / total_actual) if total_actual else None,
		"is_summary": 1,
	}


def get_actuals(rows, filters):
	"""Load actual demand once per (frequency, warehouse) group."""
	groups: dict[tuple, dict] = {}
	for row in rows:
		key = (row.frequency, row.parent_warehouse)
		group = groups.setdefault(key, {"items": set(), "min_date": None, "max_date": None})
		group["items"].add(row.item_code)
		date = getdate(row.delivery_date)
		if group["min_date"] is None or date < group["min_date"]:
			group["min_date"] = date
		if group["max_date"] is None or date > group["max_date"]:
			group["max_date"] = date

	actuals = {}
	for (frequency, warehouse), group in groups.items():
		actuals[(frequency, warehouse)] = loaders.get_demand_history(
			company=filters.company,
			from_date=period_start(group["min_date"], frequency),
			to_date=group["max_date"],
			based_on=filters.get("based_on") or "Sales Order",
			periodicity=frequency,
			item_code=sorted(group["items"]),
			warehouse=warehouse,
		)

	return actuals


def get_columns():
	return [
		{
			"label": _("Sales Forecast"),
			"fieldname": "sales_forecast",
			"fieldtype": "Link",
			"options": "Sales Forecast",
			"width": 150,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 140,
		},
		{"label": _("Item Name"), "fieldname": "item_name", "fieldtype": "Data", "width": 130},
		{"label": _("Period"), "fieldname": "period", "fieldtype": "Date", "width": 100},
		{"label": _("Model"), "fieldname": "forecast_model", "fieldtype": "Data", "width": 130},
		{"label": _("Forecast Qty"), "fieldname": "forecast_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Actual Qty"), "fieldname": "actual_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Abs Error"), "fieldname": "abs_error", "fieldtype": "Float", "width": 100},
		{"label": _("APE"), "fieldname": "ape", "fieldtype": "Percent", "width": 90},
		{"label": _("MAPE"), "fieldname": "mape", "fieldtype": "Percent", "width": 90},
		{"label": _("Bias"), "fieldname": "bias", "fieldtype": "Percent", "width": 90},
	]


def get_chart_data(data):
	detail_rows = [row for row in data if not row.get("is_summary")]
	if not detail_rows:
		return None

	period_totals: dict = {}
	for row in detail_rows:
		totals = period_totals.setdefault(row["period"], {"forecast": 0.0, "actual": 0.0})
		totals["forecast"] += row["forecast_qty"]
		totals["actual"] += row["actual_qty"]

	periods = sorted(period_totals)
	return {
		"data": {
			"labels": [frappe.format(period, {"fieldtype": "Date"}) for period in periods],
			"datasets": [
				{"name": _("Forecast"), "values": [period_totals[p]["forecast"] for p in periods]},
				{"name": _("Actual"), "values": [period_totals[p]["actual"] for p in periods]},
			],
		},
		"type": "line",
	}


def get_report_summary(data):
	detail_rows = [row for row in data if not row.get("is_summary")]
	if not detail_rows:
		return None

	apes = [row["ape"] for row in detail_rows if row["ape"] is not None]
	total_actual = sum(row["actual_qty"] for row in detail_rows)
	total_error = sum(row["error"] for row in detail_rows)

	return [
		{
			"value": (sum(apes) / len(apes)) if apes else 0.0,
			"label": _("Overall MAPE"),
			"datatype": "Percent",
			"indicator": "Blue",
		},
		{
			"value": (100.0 * total_error / total_actual) if total_actual else 0.0,
			"label": _("Overall Bias"),
			"datatype": "Percent",
			"indicator": "Orange" if total_actual and abs(total_error / total_actual) > 0.1 else "Green",
		},
		{
			"value": len(detail_rows),
			"label": _("Periods Scored"),
			"datatype": "Int",
			"indicator": "Green",
		},
	]
