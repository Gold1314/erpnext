# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the cash-flow forecasting engine.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see erpnext/manufacturing/scheduling for the
pattern this module follows).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Periodicities
DAILY = "Daily"
WEEKLY = "Weekly"
MONTHLY = "Monthly"
PERIODICITIES = (DAILY, WEEKLY, MONTHLY)

# Source types
RECEIVABLE = "Receivable"
PAYABLE = "Payable"
SALES_ORDER = "Sales Order"
PURCHASE_ORDER = "Purchase Order"
SUBSCRIPTION = "Subscription"
OPENING_BALANCE = "Opening Balance"

#: sources that are committed money (invoiced), as opposed to pipeline
COMMITTED_SOURCES = (RECEIVABLE, PAYABLE)
#: sources that are pipeline / uncertain - scenario haircut applies to these
PIPELINE_SOURCES = (SALES_ORDER, PURCHASE_ORDER)

SOURCE_TYPES = (RECEIVABLE, PAYABLE, SALES_ORDER, PURCHASE_ORDER, SUBSCRIPTION, OPENING_BALANCE)


@dataclass
class CashFlowItem:
	"""One expected cash movement. Positive amount = inflow, negative = outflow.

	``amount`` is expected in a single common currency (loaders convert to the
	company currency); ``currency`` records which one for display.
	"""

	posting_date: datetime.date
	amount: float
	source_type: str
	party: str | None = None
	party_type: str | None = None
	reference_doctype: str | None = None
	reference_name: str | None = None
	bank_account: str | None = None
	currency: str | None = None


@dataclass
class ForecastScenario:
	"""What-if knobs applied by the engine before bucketing.

	- ``receivable_delay_days`` shifts expected customer receipts
	  (:data:`RECEIVABLE` items) later (or earlier when negative).
	- ``payable_delay_days`` does the same for :data:`PAYABLE` items.
	- ``include_sales_orders`` / ``include_purchase_orders`` drop the
	  respective pipeline items entirely.
	- ``confidence_haircut_pct`` scales pipeline items
	  (:data:`PIPELINE_SOURCES`) down by N percent to reflect uncertainty.
	"""

	receivable_delay_days: int = 0
	payable_delay_days: int = 0
	include_sales_orders: bool = True
	include_purchase_orders: bool = True
	confidence_haircut_pct: float = 0.0


@dataclass
class ForecastPeriod:
	"""One bucket of the forecast timeline (dates are inclusive)."""

	key: str
	label: str
	from_date: datetime.date
	to_date: datetime.date
	#: gross inflow per source type (positive numbers)
	inflows: dict[str, float] = field(default_factory=dict)
	#: gross outflow per source type (positive numbers)
	outflows: dict[str, float] = field(default_factory=dict)
	opening_balance: float = 0.0
	closing_balance: float = 0.0

	@property
	def total_inflow(self) -> float:
		return sum(self.inflows.values())

	@property
	def total_outflow(self) -> float:
		return sum(self.outflows.values())

	@property
	def net(self) -> float:
		return self.total_inflow - self.total_outflow

	def add(self, source_type: str, amount: float) -> None:
		bucket = self.inflows if amount >= 0 else self.outflows
		bucket[source_type] = bucket.get(source_type, 0.0) + abs(amount)


@dataclass
class ForecastResult:
	periods: list[ForecastPeriod] = field(default_factory=list)
	opening_balance: float = 0.0
	periodicity: str = WEEKLY

	@property
	def closing_balance(self) -> float:
		return self.periods[-1].closing_balance if self.periods else self.opening_balance

	def inflow_source_types(self) -> list[str]:
		return self._source_types("inflows")

	def outflow_source_types(self) -> list[str]:
		return self._source_types("outflows")

	def _source_types(self, side: str) -> list[str]:
		seen = {source for period in self.periods for source in getattr(period, side)}
		ordered = [source for source in SOURCE_TYPES if source in seen]
		ordered.extend(sorted(seen - set(ordered)))
		return ordered
