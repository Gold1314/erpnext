# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for WMS-lite (bin locations + cycle counting).

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see erpnext/stock/promising and
erpnext/manufacturing/scheduling for the pattern this module follows).

All quantities are in the item's **stock UOM** (loaders convert; storage
location capacity is likewise interpreted in stock UOM for v1).
"""

from __future__ import annotations

from dataclasses import dataclass

# Storage Location types (mirror the ``location_type`` Select options on the
# Storage Location doctype)
ZONE = "Zone"
AISLE = "Aisle"
RACK = "Rack"
BIN = "Bin"
STAGING = "Staging"
QC = "QC"
BULK = "Bulk"
PICK_FACE = "Pick Face"

LOCATION_TYPES = (ZONE, AISLE, RACK, BIN, STAGING, QC, BULK, PICK_FACE)

# ABC classes
CLASS_A = "A"
CLASS_B = "B"
CLASS_C = "C"


@dataclass
class LocationInfo:
	"""One leaf storage location as the engine sees it.

	``capacity_qty`` of 0 means *unlimited* capacity. ``current_qty`` is the
	total stock currently in the location (across items - capacity is a
	simple qty budget in v1), or the qty of one specific item when the
	caller builds pick candidates via ``loaders.get_location_stock``.
	"""

	code: str
	warehouse: str
	location_type: str = BIN
	pick_sequence: int = 0
	capacity_qty: float = 0.0
	current_qty: float = 0.0


@dataclass
class PutawaySuggestion:
	"""Put ``qty`` into ``location_code``."""

	location_code: str
	qty: float


@dataclass
class PickSuggestion:
	"""Pick ``qty`` from ``location_code`` (visited in ``pick_sequence`` order)."""

	location_code: str
	qty: float
	pick_sequence: int = 0
