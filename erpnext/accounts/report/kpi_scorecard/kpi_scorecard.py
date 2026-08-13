# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""KPI Scorecard - the front door to the semantic metric layer.

Every number on this report comes from ``erpnext.analytics.api.get_kpis``, so
the report cannot drift from the API, the snapshots or an agent's answer: they
all read the same catalog (``erpnext/analytics/registry.py``) and the same
loaders.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, fmt_money, get_first_day, get_last_day, getdate, today

from erpnext.analytics.api import compute_kpis
from erpnext.analytics.models import (
	AMBER,
	CATEGORIES,
	COUNT,
	CURRENCY,
	DAYS,
	GREEN,
	PERCENT,
	RATIO,
	RED,
	UNKNOWN,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.company:
		frappe.throw(_("Please select a Company."))

	period_start, period_end = resolve_period(filters)
	currency = frappe.get_cached_value("Company", filters.company, "default_currency")

	rows = compute_kpis(filters.company, period_start, period_end)
	if filters.get("category"):
		rows = [row for row in rows if row["category"] == filters.category]

	data = build_rows(rows, currency)
	chart = get_chart(rows)
	summary = get_summary(rows)
	message = get_message(rows, period_start, period_end)

	return get_columns(), data, message, chart, summary


def resolve_period(filters):
	period_end = getdate(filters.period_end) if filters.period_end else get_last_day(today())
	period_start = getdate(filters.period_start) if filters.period_start else get_first_day(period_end)
	if period_start > period_end:
		frappe.throw(_("Period Start Date cannot be after Period End Date."))
	return period_start, period_end


def get_columns():
	return [
		{"label": _("Metric"), "fieldname": "metric", "fieldtype": "Data", "width": 240},
		{"label": _("Value"), "fieldname": "value", "fieldtype": "Data", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Prior Period"), "fieldname": "prior_value", "fieldtype": "Data", "width": 130},
		{"label": _("Change"), "fieldname": "change", "fieldtype": "Data", "width": 120},
		{"label": _("% Change"), "fieldname": "pct_change", "fieldtype": "Data", "width": 100},
		{"label": _("Formula"), "fieldname": "formula", "fieldtype": "Data", "width": 380},
		{"label": _("Key"), "fieldname": "metric_key", "fieldtype": "Data", "width": 180},
	]


def build_rows(rows, currency):
	"""One bold row per category, then its metrics - the pack's own grouping."""
	by_category = {}
	for row in rows:
		by_category.setdefault(row["category"], []).append(row)

	data = []
	for category in CATEGORIES:
		metrics = by_category.get(category)
		if not metrics:
			continue

		data.append({"metric": _(category), "is_group": 1})
		for row in metrics:
			data.append(
				{
					"metric": row["label"],
					"metric_key": row["key"],
					"value": format_value(row["value"], row["unit"], currency),
					"status": row["status"],
					"prior_value": format_value(row["prior_value"], row["unit"], currency),
					"change": format_change(row, currency),
					"pct_change": format_pct_change(row["pct_change"]),
					"formula": row["formula_text"],
					"indent": 1,
					# consumed by the report's JS formatter, not shown as columns
					"direction_is_good": row["direction_is_good"],
					"warning_count": len(row["warnings"]),
				}
			)

	return data


def format_value(value, unit, currency):
	if value is None:
		return _("n/a")
	if unit == CURRENCY:
		return fmt_money(value, currency=currency)
	if unit == PERCENT:
		return f"{value:.2f}%"
	if unit == DAYS:
		return _("{0} days").format(f"{value:.1f}")
	if unit == RATIO:
		return f"{value:.3f}"
	if unit == COUNT:
		return f"{int(value)}"
	return str(value)


def format_change(row, currency):
	change = row["change"]
	if change is None:
		return _("n/a")
	prefix = "+" if change > 0 else ""
	return prefix + format_value(change, row["unit"], currency)


def format_pct_change(pct_change):
	if pct_change is None:
		return _("n/a")
	prefix = "+" if pct_change > 0 else ""
	return f"{prefix}{pct_change:.2f}%"


def get_chart(rows):
	"""Status mix - the one-glance answer to 'how healthy is this company?'."""
	counts = {status: 0 for status in (GREEN, AMBER, RED, UNKNOWN)}
	for row in rows:
		counts[row["status"]] = counts.get(row["status"], 0) + 1

	return {
		"data": {
			"labels": [_(status) for status in (GREEN, AMBER, RED, UNKNOWN)],
			"datasets": [{"name": _("Metrics"), "values": [counts[s] for s in (GREEN, AMBER, RED, UNKNOWN)]}],
		},
		"type": "bar",
		"colors": ["#28a745", "#ff9800", "#dc3545", "#adb5bd"],
		"barOptions": {"spaceRatio": 0.4},
	}


def get_summary(rows):
	counts = {status: 0 for status in (GREEN, AMBER, RED, UNKNOWN)}
	for row in rows:
		counts[row["status"]] = counts.get(row["status"], 0) + 1

	return [
		{"label": _("Green"), "value": counts[GREEN], "indicator": "Green", "datatype": "Int"},
		{"label": _("Amber"), "value": counts[AMBER], "indicator": "Orange", "datatype": "Int"},
		{"label": _("Red"), "value": counts[RED], "indicator": "Red", "datatype": "Int"},
		{"label": _("Not Graded"), "value": counts[UNKNOWN], "indicator": "Grey", "datatype": "Int"},
	]


def get_message(rows, period_start, period_end):
	"""Surface loader warnings once, instead of per metric."""
	warnings = []
	for row in rows:
		for warning in row["warnings"]:
			if warning not in warnings:
				warnings.append(warning)

	header = _("Period {0} to {1}, compared with the preceding period of equal length.").format(
		frappe.format(period_start, {"fieldtype": "Date"}),
		frappe.format(period_end, {"fieldtype": "Date"}),
	)
	if not warnings:
		return f"<p>{header}</p>"

	items = "".join(f"<li>{escape_html(w)}</li>" for w in warnings[:15])
	return (
		f"<p>{header}</p>"
		f"<p class='text-muted'>{_('Data notes')}:</p>"
		f"<ul class='text-muted small'>{items}</ul>"
	)
