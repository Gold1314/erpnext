# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the WMS-lite engine.

All DB access lives here; the engine (``engine.py``) stays pure. Location
stock is read from ``tabStock Ledger Entry`` grouped by the custom column the
Inventory Dimension machinery generates for Storage Location:
``InventoryDimension.set_source_and_target_fieldname`` sets both source and
target fieldname to ``scrub(dimension_name)``, so the dimension named
"Storage Location" lands in SLE column ``storage_location``. The actual
column name is always read back from the Inventory Dimension record rather
than re-derived, and loaders that need it raise
:class:`WMSDimensionNotInstalled` with a pointer to the installer when no
such dimension exists yet.

Every fieldname used below is verified against the doctype JSONs - see the
field table in ``DESIGN.md``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, flt, getdate, nowdate

from erpnext.stock.wms.models import LocationInfo

DIMENSION_REFERENCE_DOCTYPE = "Storage Location"


class WMSDimensionNotInstalled(frappe.ValidationError):
	pass


def get_dimension_fieldname(throw: bool = False) -> str | None:
	"""SLE column name of the Storage Location inventory dimension, if installed."""
	fieldname = frappe.db.get_value(
		"Inventory Dimension",
		{"reference_document": DIMENSION_REFERENCE_DOCTYPE},
		"target_fieldname",
	)

	if not fieldname and throw:
		frappe.throw(
			_(
				"The Storage Location inventory dimension is not installed yet, so stock is not"
				" tracked per location. Ask a System Manager to run"
				" erpnext.stock.wms.api.setup_wms_dimension first."
			),
			WMSDimensionNotInstalled,
			title=_("WMS Dimension Missing"),
		)

	return fieldname


def get_locations(warehouse: str, include_current_qty: bool = True) -> list[LocationInfo]:
	"""Active leaf Storage Locations of a warehouse, as engine inputs.

	``current_qty`` is the total on-location stock across items (capacity is
	a plain qty budget in v1), summed from SLE by the dimension column. When
	the dimension is not installed yet the locations are still returned -
	with ``current_qty`` 0 - so putaway suggestions degrade gracefully to
	pure capacity/type/sequence ordering.
	"""
	rows = frappe.get_all(
		"Storage Location",
		filters={"warehouse": warehouse, "is_group": 0, "disabled": 0},
		fields=["name", "warehouse", "location_type", "pick_sequence", "capacity_qty"],
		order_by="pick_sequence asc, name asc",
	)

	qty_by_location: dict[str, float] = {}
	if rows and include_current_qty and get_dimension_fieldname():
		qty_by_location = _get_location_totals(warehouse)

	return [
		LocationInfo(
			code=row.name,
			warehouse=row.warehouse,
			location_type=row.location_type,
			pick_sequence=int(row.pick_sequence or 0),
			capacity_qty=flt(row.capacity_qty),
			current_qty=flt(qty_by_location.get(row.name)),
		)
		for row in rows
	]


def _get_location_totals(warehouse: str) -> dict[str, float]:
	"""Total qty per location (all items) in one warehouse, from SLE."""
	fieldname = get_dimension_fieldname(throw=True)

	sle = frappe.qb.DocType("Stock Ledger Entry")
	location_field = getattr(sle, fieldname)

	rows = (
		frappe.qb.from_(sle)
		.select(location_field.as_("location"), Sum(sle.actual_qty).as_("qty"))
		.where(
			(sle.warehouse == warehouse)
			& (sle.is_cancelled == 0)
			& (location_field.isnotnull())
			& (location_field != "")
		)
		.groupby(location_field)
	).run(as_dict=True)

	return {row.location: flt(row.qty) for row in rows}


def get_location_stock(item_code: str, warehouse: str) -> dict[str, float]:
	"""Qty of one item per storage location in a warehouse.

	Summed from ``tabStock Ledger Entry`` grouped by the dimension column;
	raises :class:`WMSDimensionNotInstalled` (with a clear message) when the
	Storage Location dimension has not been installed.
	"""
	fieldname = get_dimension_fieldname(throw=True)

	sle = frappe.qb.DocType("Stock Ledger Entry")
	location_field = getattr(sle, fieldname)

	rows = (
		frappe.qb.from_(sle)
		.select(location_field.as_("location"), Sum(sle.actual_qty).as_("qty"))
		.where(
			(sle.item_code == item_code)
			& (sle.warehouse == warehouse)
			& (sle.is_cancelled == 0)
			& (location_field.isnotnull())
			& (location_field != "")
		)
		.groupby(location_field)
	).run(as_dict=True)

	return {row.location: flt(row.qty) for row in rows}


def get_item_velocity(company: str, warehouse: str, lookback_days: int = 90) -> dict[str, float]:
	"""Outgoing consumption value per item over the lookback window.

	Velocity = ``sum(-actual_qty * valuation_rate)`` of non-cancelled
	outgoing SLE rows (``actual_qty < 0``). A group warehouse expands to its
	leaf descendants via lft/rgt.
	"""
	from_date = add_days(getdate(nowdate()), -abs(int(lookback_days or 90)))

	sle = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(sle)
		.select(sle.item_code, Sum(-sle.actual_qty * sle.valuation_rate).as_("velocity_value"))
		.where(
			(sle.company == company)
			& (sle.is_cancelled == 0)
			& (sle.actual_qty < 0)
			& (sle.posting_date >= from_date)
			& (sle.warehouse.isin(get_leaf_warehouses(warehouse)))
		)
		.groupby(sle.item_code)
	)

	return {row.item_code: flt(row.velocity_value) for row in query.run(as_dict=True)}


def get_stocked_items(warehouse: str) -> list[str]:
	"""Items with a non-zero Bin balance anywhere under the warehouse."""
	rows = frappe.get_all(
		"Bin",
		filters={"warehouse": ("in", get_leaf_warehouses(warehouse)), "actual_qty": ("!=", 0)},
		pluck="item_code",
		distinct=True,
	)
	return rows


def get_leaf_warehouses(warehouse: str) -> list[str]:
	"""The warehouse itself, or its leaf descendants when it is a group."""
	details = frappe.db.get_value("Warehouse", warehouse, ["lft", "rgt", "is_group"], as_dict=True)
	if not details:
		frappe.throw(_("Warehouse {0} not found").format(warehouse))

	if not details.is_group:
		return [warehouse]

	return frappe.get_all(
		"Warehouse",
		filters={"lft": (">=", details.lft), "rgt": ("<=", details.rgt), "is_group": 0},
		pluck="name",
	)
