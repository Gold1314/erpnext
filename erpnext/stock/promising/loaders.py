# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the ATP promise engine.

All DB access lives here; the engine (``engine.py``) stays pure. Every loader
returns event lists with quantities in the item's **stock UOM** (child-table
``qty`` fields are multiplied by ``conversion_factor`` where the doctype
carries one). Every fieldname used below is verified against the doctype
JSONs - see the field table in ``DESIGN.md``.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext.stock.promising.engine import DEFAULT_HORIZON_DAYS
from erpnext.stock.promising.models import (
	ON_HAND,
	PURCHASE_ORDER,
	WORK_ORDER,
	DemandEvent,
	SupplyEvent,
)

#: Work Order statuses that no longer produce supply (mirrors the MRP report)
CLOSED_WORK_ORDER_STATUSES = ("Stopped", "Closed", "Completed")
#: Purchase Order statuses whose pending qty should not be counted as supply
CLOSED_PURCHASE_ORDER_STATUSES = ("Closed", "On Hold")
#: Sales Order statuses whose undelivered qty no longer commits demand
CLOSED_SALES_ORDER_STATUSES = ("Closed",)


def get_horizon_end(today=None, horizon_days: int = DEFAULT_HORIZON_DAYS):
	return add_days(getdate(today or nowdate()), horizon_days)


def get_supply_events(item_code: str, warehouse: str, company: str | None = None) -> list[SupplyEvent]:
	"""All supply for one (item, warehouse): on hand + open POs + open WOs.

	Horizon filtering happens in the engine, so callers can reuse one load
	for several horizon settings.
	"""
	return (
		get_on_hand(item_code, warehouse, company)
		+ get_open_purchase_orders(item_code, warehouse, company)
		+ get_open_work_orders(item_code, warehouse, company)
	)


def get_on_hand(item_code: str, warehouse: str, company: str | None = None) -> list[SupplyEvent]:
	"""Free stock on hand today: Bin ``actual_qty`` - ``reserved_stock``.

	``reserved_stock`` is the qty hard-reserved via Stock Reservation
	Entries (the Bin controller sums non-delivered SRE qty into it), so
	those units are excluded here. Soft commitments (``reserved_qty`` from
	open Sales Orders) are deliberately *not* subtracted - they enter the
	engine as :class:`DemandEvent` rows via :func:`get_committed_demand`
	instead, dated by delivery date rather than lumped into day zero.
	``company`` is unused (Bin is item x warehouse; the warehouse implies
	the company) but kept for signature symmetry.
	"""
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "reserved_stock"],
		as_dict=True,
	)
	if not row:
		return []

	available = flt(row.actual_qty) - flt(row.reserved_stock)
	if available <= 0:
		return []

	return [
		SupplyEvent(
			available_date=getdate(nowdate()),
			qty=available,
			source_type=ON_HAND,
			reference=None,
		)
	]


def get_open_purchase_orders(
	item_code: str, warehouse: str, company: str | None = None
) -> list[SupplyEvent]:
	"""Pending qty of submitted PO rows into this warehouse, on ``schedule_date``.

	Pending = ``(qty - received_qty) * conversion_factor`` (stock UOM),
	following the MRP report's PO supply query. Row-level ``schedule_date``
	("Required By") is used, falling back to the row's
	``expected_delivery_date`` and then the parent ``schedule_date``.
	"""
	po = frappe.qb.DocType("Purchase Order")
	po_item = frappe.qb.DocType("Purchase Order Item")

	query = (
		frappe.qb.from_(po)
		.inner_join(po_item)
		.on(po.name == po_item.parent)
		.select(
			po.name,
			po_item.schedule_date,
			po_item.expected_delivery_date,
			po.schedule_date.as_("parent_schedule_date"),
			((po_item.qty - po_item.received_qty) * po_item.conversion_factor).as_("pending_qty"),
		)
		.where(
			(po.docstatus == 1)
			& (po.status.notin(list(CLOSED_PURCHASE_ORDER_STATUSES)))
			& (po_item.item_code == item_code)
			& (po_item.warehouse == warehouse)
			& (po_item.qty > po_item.received_qty)
		)
	)
	if company:
		query = query.where(po.company == company)

	rows = query.run(as_dict=True)

	return [
		SupplyEvent(
			available_date=getdate(row.schedule_date or row.expected_delivery_date or row.parent_schedule_date),
			qty=flt(row.pending_qty),
			source_type=PURCHASE_ORDER,
			reference=row.name,
		)
		for row in rows
	]


def get_open_work_orders(item_code: str, warehouse: str, company: str | None = None) -> list[SupplyEvent]:
	"""Pending qty of submitted Work Orders producing this item into this warehouse.

	Pending = ``qty - produced_qty`` (Work Order quantities are already in
	stock UOM). Available on ``expected_delivery_date``, falling back to the
	date part of ``planned_end_date`` (a Datetime field), then today.
	"""
	wo = frappe.qb.DocType("Work Order")

	query = (
		frappe.qb.from_(wo)
		.select(
			wo.name,
			wo.expected_delivery_date,
			wo.planned_end_date,
			(wo.qty - wo.produced_qty).as_("pending_qty"),
		)
		.where(
			(wo.docstatus == 1)
			& (wo.status.notin(list(CLOSED_WORK_ORDER_STATUSES)))
			& (wo.production_item == item_code)
			& (wo.fg_warehouse == warehouse)
			& (wo.qty > wo.produced_qty)
		)
	)
	if company:
		query = query.where(wo.company == company)

	rows = query.run(as_dict=True)

	return [
		SupplyEvent(
			available_date=getdate(row.expected_delivery_date or row.planned_end_date or nowdate()),
			qty=flt(row.pending_qty),
			source_type=WORK_ORDER,
			reference=row.name,
		)
		for row in rows
	]


def get_committed_demand(
	item_code: str,
	warehouse: str,
	company: str | None = None,
	exclude_sales_order: str | None = None,
) -> list[DemandEvent]:
	"""Undelivered qty of open submitted Sales Order rows for this (item, warehouse).

	Pending = ``(qty - delivered_qty) * conversion_factor`` (stock UOM).
	Required on the child ``delivery_date``, falling back to the parent
	``delivery_date`` and then the parent ``transaction_date``.
	``exclude_sales_order`` drops the order currently being promised so it
	does not compete with itself. v1 keeps demand to Sales Orders only -
	Material Request and Production Plan raw-material demand are noted as
	follow-ups in ``DESIGN.md``.
	"""
	so = frappe.qb.DocType("Sales Order")
	so_item = frappe.qb.DocType("Sales Order Item")

	query = (
		frappe.qb.from_(so)
		.inner_join(so_item)
		.on(so.name == so_item.parent)
		.select(
			so.name,
			so_item.delivery_date,
			so.delivery_date.as_("parent_delivery_date"),
			so.transaction_date,
			((so_item.qty - so_item.delivered_qty) * so_item.conversion_factor).as_("pending_qty"),
		)
		.where(
			(so.docstatus == 1)
			& (so.status.notin(list(CLOSED_SALES_ORDER_STATUSES)))
			& (so_item.item_code == item_code)
			& (so_item.warehouse == warehouse)
			& (so_item.qty > so_item.delivered_qty)
		)
	)
	if company:
		query = query.where(so.company == company)
	if exclude_sales_order:
		query = query.where(so.name != exclude_sales_order)

	return [
		DemandEvent(
			required_date=getdate(row.delivery_date or row.parent_delivery_date or row.transaction_date),
			qty=flt(row.pending_qty),
			reference=row.name,
		)
		for row in query.run(as_dict=True)
	]
