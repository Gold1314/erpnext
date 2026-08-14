# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the ATP promise engine.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see erpnext/manufacturing/scheduling and
erpnext/accounts/forecasting for the pattern this module follows).

All quantities are expected in the item's **stock UOM** (loaders convert).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Supply source types
ON_HAND = "On Hand"
PURCHASE_ORDER = "Purchase Order"
WORK_ORDER = "Work Order"

SUPPLY_SOURCE_TYPES = (ON_HAND, PURCHASE_ORDER, WORK_ORDER)


@dataclass
class SupplyEvent:
	"""Quantity of an item becoming available in a warehouse on a date.

	``available_date`` is the date the quantity can first satisfy demand
	(today for on-hand stock, ``schedule_date`` for a PO row, expected end
	for a Work Order). ``reference`` names the source document, if any.
	"""

	available_date: datetime.date
	qty: float
	source_type: str
	reference: str | None = None


@dataclass
class DemandEvent:
	"""Already-committed demand (e.g. an open submitted Sales Order row).

	Existing demand is netted against supply *before* a new request is
	evaluated, so the engine never promises the same unit twice.
	"""

	required_date: datetime.date
	qty: float
	reference: str | None = None


@dataclass
class PromiseRequest:
	"""One "when can I have N of item X from warehouse W?" question."""

	item_code: str
	warehouse: str
	qty: float
	requested_date: datetime.date | None = None


@dataclass
class Allocation:
	"""One slice of supply covering (part of) a promise."""

	source_type: str
	reference: str | None
	available_date: datetime.date
	qty: float


@dataclass
class PromiseResult:
	item_code: str
	qty: float
	promised_date: datetime.date | None = None
	fulfillable: bool = False
	shortfall: float = 0.0
	allocation: list[Allocation] = field(default_factory=list)
	message: str = ""
