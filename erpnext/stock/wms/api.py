# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for WMS-lite.

``setup_wms_dimension`` is the installer that promotes Storage Location into
a real stock-ledger dimension via the existing Inventory Dimension machinery
(its ``on_update`` generates the custom fields on every inventory document
plus Stock Ledger Entry / Stock Closing Balance - no doctype JSON edits).
The suggestion endpoints are thin adapters over the pure engine.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.stock.wms import loaders
from erpnext.stock.wms.engine import suggest_picks, suggest_putaway
from erpnext.stock.wms.loaders import DIMENSION_REFERENCE_DOCTYPE
from erpnext.stock.wms.models import LocationInfo

DIMENSION_NAME = "Storage Location"


@frappe.whitelist()
def setup_wms_dimension() -> dict:
	"""Idempotently create the Storage Location Inventory Dimension.

	Creates an Inventory Dimension with ``reference_document`` "Storage
	Location" and ``apply_to_all_doctypes`` 1 (its ``before_save`` then sets
	``type_of_transaction`` "Both" and derives ``source_fieldname`` /
	``target_fieldname`` as ``scrub(dimension_name)`` = ``storage_location``;
	``istable`` / ``document_type`` / ``condition`` are reset by the
	controller for apply-to-all dimensions, so they are not set here).
	``reqd`` stays 0: locations are opt-in per row until a site is ready to
	direct every movement.

	System Manager only. Returns what exists/was created, including the SLE
	column name (``target_fieldname``).
	"""
	frappe.only_for("System Manager")

	existing = frappe.db.get_value(
		"Inventory Dimension",
		{"reference_document": DIMENSION_REFERENCE_DOCTYPE},
		["name", "target_fieldname", "source_fieldname"],
		as_dict=True,
	)
	if existing:
		return {
			"created": False,
			"dimension": existing.name,
			"target_fieldname": existing.target_fieldname,
			"source_fieldname": existing.source_fieldname,
			"message": _("Inventory Dimension {0} already exists.").format(existing.name),
		}

	dimension = frappe.get_doc(
		{
			"doctype": "Inventory Dimension",
			"dimension_name": DIMENSION_NAME,
			"reference_document": DIMENSION_REFERENCE_DOCTYPE,
			"apply_to_all_doctypes": 1,
			"reqd": 0,
			"validate_negative_stock": 0,
		}
	).insert()

	return {
		"created": True,
		"dimension": dimension.name,
		"target_fieldname": dimension.target_fieldname,
		"source_fieldname": dimension.source_fieldname,
		"message": _(
			"Inventory Dimension {0} created. Storage Location fields are now available on all"
			" inventory documents and the Stock Ledger."
		).format(dimension.name),
	}


@frappe.whitelist()
def suggest_putaway_location(item_code: str, warehouse: str, qty: float) -> dict:
	"""Directed putaway: where should ``qty`` of ``item_code`` go in ``warehouse``?

	Loads active leaf locations (with their current total stock when the
	dimension is installed) and fills by remaining capacity, preferring
	Bin/Pick Face/Bulk over Staging/QC and lower pick sequence among equals.
	``item_code`` is accepted for parity with Putaway Rule bridging (a
	follow-up); v1 capacity is item-agnostic.
	"""
	frappe.has_permission("Storage Location", "read", throw=True)

	locations = loaders.get_locations(warehouse)
	if not locations:
		return {
			"suggestions": [],
			"unallocated_qty": flt(qty),
			"message": _("No active storage locations found for warehouse {0}.").format(warehouse),
		}

	suggestions, remainder = suggest_putaway(locations, flt(qty))

	return {
		"suggestions": [{"storage_location": s.location_code, "qty": s.qty} for s in suggestions],
		"unallocated_qty": remainder,
		"message": _("Insufficient location capacity for {0} units.").format(remainder)
		if remainder
		else "",
	}


@frappe.whitelist()
def suggest_pick_locations(item_code: str, warehouse: str, qty: float) -> dict:
	"""Directed picking: where to take ``qty`` of ``item_code`` from, in pick-path order.

	Requires the Storage Location dimension to be installed (location-level
	stock comes from the SLE dimension column); otherwise a clear error
	points at :func:`setup_wms_dimension`.
	"""
	frappe.has_permission("Storage Location", "read", throw=True)

	stock_by_location = loaders.get_location_stock(item_code, warehouse)

	meta_by_location = {
		row.name: row
		for row in frappe.get_all(
			"Storage Location",
			filters={"name": ("in", list(stock_by_location))},
			fields=["name", "warehouse", "location_type", "pick_sequence", "capacity_qty", "disabled"],
		)
	}

	candidates = []
	for location, location_qty in stock_by_location.items():
		meta = meta_by_location.get(location)
		if meta is None or meta.disabled:
			continue
		candidates.append(
			LocationInfo(
				code=location,
				warehouse=meta.warehouse,
				location_type=meta.location_type,
				pick_sequence=int(meta.pick_sequence or 0),
				capacity_qty=flt(meta.capacity_qty),
				current_qty=flt(location_qty),
			)
		)

	suggestions, shortfall = suggest_picks(candidates, flt(qty))

	return {
		"suggestions": [
			{"storage_location": s.location_code, "qty": s.qty, "pick_sequence": s.pick_sequence}
			for s in suggestions
		],
		"shortfall_qty": shortfall,
		"message": _("{0} units are not available in any storage location.").format(shortfall)
		if shortfall
		else "",
	}
