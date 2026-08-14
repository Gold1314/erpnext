# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for ATP order promising.

Thin adapters over the pure engine: load supply/demand via ``loaders.py``,
run ``engine.promise`` / ``engine.promise_many``, serialize results. Only
:func:`apply_promise_dates` writes anything, and only to a draft Sales Order.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate, parse_json

from erpnext.stock.promising import loaders
from erpnext.stock.promising.engine import DEFAULT_HORIZON_DAYS, promise, promise_many
from erpnext.stock.promising.models import PromiseRequest


@frappe.whitelist()
def get_promise_date(
	item_code: str,
	warehouse: str,
	qty: float,
	company: str | None = None,
	requested_date: str | None = None,
	horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict:
	"""Earliest promise date for ``qty`` (stock UOM) of an item in a warehouse."""
	frappe.has_permission("Bin", "read", throw=True)

	today = getdate(nowdate())
	horizon_days = cint(horizon_days) or DEFAULT_HORIZON_DAYS

	result = promise(
		PromiseRequest(
			item_code=item_code,
			warehouse=warehouse,
			qty=flt(qty),
			requested_date=getdate(requested_date) if requested_date else None,
		),
		loaders.get_supply_events(item_code, warehouse, company),
		loaders.get_committed_demand(item_code, warehouse, company),
		horizon_end=loaders.get_horizon_end(today, horizon_days),
		today=today,
	)

	return result_to_dict(result)


@frappe.whitelist()
def promise_sales_order(sales_order: str, horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[dict]:
	"""Compute promise dates for every item row of a **draft** Sales Order.

	Read-only: nothing is written. Rows are allocated sequentially in row
	order, sharing one supply pool per (item, warehouse), so repeated items
	do not double-count. Returns one dict per item row.
	"""
	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("read")

	if doc.docstatus != 0:
		frappe.throw(_("Promise dates can only be computed for a draft Sales Order."))

	today = getdate(nowdate())
	horizon_end = loaders.get_horizon_end(today, cint(horizon_days) or DEFAULT_HORIZON_DAYS)

	requests = []
	supply_by_key = {}
	demand_by_key = {}
	for row in doc.items:
		key = (row.item_code, row.warehouse)
		if key not in supply_by_key:
			supply_by_key[key] = loaders.get_supply_events(row.item_code, row.warehouse, doc.company)
			demand_by_key[key] = loaders.get_committed_demand(
				row.item_code, row.warehouse, doc.company, exclude_sales_order=doc.name
			)

		requests.append(
			PromiseRequest(
				item_code=row.item_code,
				warehouse=row.warehouse,
				qty=flt(row.qty) * flt(row.conversion_factor or 1),
				requested_date=getdate(row.delivery_date) if row.delivery_date else None,
			)
		)

	results = promise_many(requests, supply_by_key, demand_by_key, horizon_end, today)

	rows = []
	for row, result in zip(doc.items, results, strict=True):
		rows.append(
			{
				"idx": row.idx,
				"item_code": row.item_code,
				"warehouse": row.warehouse,
				"qty": flt(row.qty),
				"current_delivery_date": row.delivery_date,
				"promised_date": result.promised_date,
				"fulfillable": result.fulfillable,
				"shortfall": result.shortfall,
				"message": result.message,
			}
		)

	return rows


@frappe.whitelist()
def apply_promise_dates(sales_order: str, rows: str | list) -> str:
	"""Write promised dates onto a **draft** Sales Order's item rows.

	``rows`` is the (possibly user-reviewed) output of
	:func:`promise_sales_order`; only entries with a ``promised_date`` are
	applied, matched by ``idx``. The parent ``delivery_date`` becomes the
	max of the child dates. Requires write permission on the Sales Order.
	"""
	frappe.has_permission("Sales Order", "write", throw=True)

	doc = frappe.get_doc("Sales Order", sales_order)
	doc.check_permission("write")

	if doc.docstatus != 0:
		frappe.throw(_("Promise dates can only be applied to a draft Sales Order."))

	rows = parse_json(rows) if isinstance(rows, str) else rows
	promised_by_idx = {
		cint(row.get("idx")): getdate(row["promised_date"]) for row in rows if row.get("promised_date")
	}
	if not promised_by_idx:
		frappe.throw(_("No promised dates to apply."))

	for item in doc.items:
		if item.idx in promised_by_idx:
			item.delivery_date = promised_by_idx[item.idx]

	doc.delivery_date = max(getdate(item.delivery_date) for item in doc.items if item.delivery_date)
	doc.save()

	return doc.name


def result_to_dict(result) -> dict:
	return {
		"item_code": result.item_code,
		"qty": result.qty,
		"promised_date": result.promised_date,
		"fulfillable": result.fulfillable,
		"shortfall": result.shortfall,
		"message": result.message,
		"allocation": [
			{
				"source_type": part.source_type,
				"reference": part.reference,
				"available_date": part.available_date,
				"qty": part.qty,
			}
			for part in result.allocation
		],
	}
