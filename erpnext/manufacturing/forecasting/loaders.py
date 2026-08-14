# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the demand-forecasting engine.

All DB access lives here; the engine (``engine.py`` / ``backtest.py``) stays
pure. Mirrors the data-source approach of the Exponential Smoothing
Forecasting report: aggregate child-table ``stock_qty`` from submitted
Sales Orders / Sales Invoices / Delivery Notes by the parent's date field
(``transaction_date`` for Sales Order, ``posting_date`` for Sales Invoice
and Delivery Note).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from erpnext.manufacturing.forecasting.models import (
	MONTHLY,
	PERIODICITIES,
	TimeSeries,
	period_range,
	period_start,
)
from erpnext.stock.doctype.warehouse.warehouse import get_child_warehouses

BASED_ON_DOCTYPES = ("Sales Order", "Sales Invoice", "Delivery Note")

DATE_FIELD = {
	"Sales Order": "transaction_date",
	"Sales Invoice": "posting_date",
	"Delivery Note": "posting_date",
}


def get_demand_history(
	company: str,
	from_date,
	to_date,
	based_on: str = "Sales Order",
	periodicity: str = MONTHLY,
	item_code: str | list[str] | None = None,
	item_group: str | None = None,
	warehouse: str | None = None,
) -> dict[str, TimeSeries]:
	"""Per-item historical demand as zero-filled TimeSeries.

	Every requested item gets a series covering every period between
	``from_date`` and ``to_date`` — periods without transactions carry 0.0
	(essential for intermittency detection and seasonal indexing).

	``warehouse`` may be a group warehouse; all child warehouses under it are
	included (same behavior as the Exponential Smoothing Forecasting report).
	"""
	if based_on not in BASED_ON_DOCTYPES:
		frappe.throw(frappe._("Based On must be one of {0}").format(", ".join(BASED_ON_DOCTYPES)))
	if periodicity not in PERIODICITIES:
		frappe.throw(frappe._("Periodicity must be one of {0}").format(", ".join(PERIODICITIES)))

	item_codes = [item_code] if isinstance(item_code, str) else list(item_code or [])
	if item_group:
		group_items = frappe.get_all("Item", filters={"item_group": item_group}, pluck="name")
		item_codes = sorted(set(item_codes) | set(group_items)) if item_codes else group_items
		if not item_codes:
			return {}

	rows = _get_transaction_rows(company, from_date, to_date, based_on, item_codes, warehouse)

	periods = period_range(getdate(from_date), getdate(to_date), periodicity)
	period_index = {date: index for index, date in enumerate(periods)}

	# zero-filled buckets for every requested item, plus any item that
	# actually transacted (when no explicit item filter was given)
	buckets: dict[str, list[float]] = {code: [0.0] * len(periods) for code in item_codes}
	for row in rows:
		bucket = buckets.setdefault(row.item_code, [0.0] * len(periods))
		index = period_index.get(period_start(row.posting_date, periodicity))
		if index is not None:
			bucket[index] += flt(row.qty)

	return {
		code: TimeSeries(periodicity=periodicity, points=list(zip(periods, values, strict=True)))
		for code, values in buckets.items()
	}


def _get_transaction_rows(company, from_date, to_date, based_on, item_codes, warehouse):
	parent = frappe.qb.DocType(based_on)
	child = frappe.qb.DocType(based_on + " Item")
	date_field = DATE_FIELD[based_on]

	query = (
		frappe.qb.from_(parent)
		.inner_join(child)
		.on(parent.name == child.parent)
		.select(
			parent[date_field].as_("posting_date"),
			child.item_code,
			child.stock_qty.as_("qty"),
		)
		.where(
			(parent.docstatus == 1)
			& (parent.company == company)
			& (parent[date_field] >= getdate(from_date))
			& (parent[date_field] <= getdate(to_date))
		)
	)

	if item_codes:
		query = query.where(child.item_code.isin(item_codes))

	if warehouse:
		warehouses = get_child_warehouses(warehouse) or [warehouse]
		query = query.where(child.warehouse.isin(warehouses))

	return query.run(as_dict=True)
