# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the ASC 606 / IFRS 15 revenue engine.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see ``erpnext/accounts/forecasting`` and
``erpnext/manufacturing/scheduling`` for the pattern this module follows).

The five-step model, mapped to this package:

1. Identify the contract           -> ``Revenue Contract`` doctype (adapter)
2. Identify performance obligations -> :class:`ObligationInput`
3. Determine the transaction price -> ``transaction_price`` argument
4. Allocate the price              -> :func:`engine.allocate`
5. Recognize revenue               -> :func:`engine.build_recognition_plan`
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Satisfaction methods (ASC 606-10-25-27: over time vs point in time)
POINT_IN_TIME = "Point in Time"
OVER_TIME = "Over Time"
SATISFACTION_METHODS = (POINT_IN_TIME, OVER_TIME)

# Periodicities (v1: monthly only)
MONTHLY = "Monthly"
PERIODICITIES = (MONTHLY,)

# Contract-modification methods
PROSPECTIVE = "Prospective"
CUMULATIVE_CATCH_UP = "Cumulative Catch-up"
MODIFICATION_METHODS = (PROSPECTIVE, CUMULATIVE_CATCH_UP)


@dataclass
class ObligationInput:
	"""One performance obligation of a contract.

	- ``key`` identifies the obligation across allocation / plan / modification
	  results (the doctype adapter uses the child-row idx as string).
	- ``stated_amount`` is the contractually stated (invoiced) price for this
	  obligation; the sum of stated amounts is normally the transaction price.
	- ``ssp`` is the standalone selling price used for relative allocation.
	- ``satisfaction_method`` is :data:`POINT_IN_TIME` or :data:`OVER_TIME`.
	- Over-time obligations need ``start_date`` / ``end_date`` (inclusive
	  service window); point-in-time obligations use ``satisfied_date``
	  (``None`` = not yet satisfied, i.e. recognized "on event").
	"""

	key: str
	description: str
	stated_amount: float
	ssp: float
	satisfaction_method: str = OVER_TIME
	start_date: datetime.date | None = None
	end_date: datetime.date | None = None
	satisfied_date: datetime.date | None = None


@dataclass
class AllocationResult:
	"""Relative-SSP allocation of the transaction price to one obligation."""

	key: str
	allocated_amount: float
	allocation_pct: float


@dataclass
class RecognitionRow:
	"""One planned recognition posting for an obligation.

	Over-time obligations get one row per (partial) calendar month;
	point-in-time obligations get a single row whose ``period_start`` /
	``period_end`` both equal the satisfied date - or both ``None`` when the
	satisfying event has not happened yet ("on event" rows, listed in
	:attr:`ContractPlan.event_pending_keys`).
	"""

	key: str
	period_start: datetime.date | None
	period_end: datetime.date | None
	amount: float


@dataclass
class ContractPlan:
	"""Steps 4 + 5 output: allocation plus the per-period recognition plan.

	Invariant (tested): ``sum(row.amount for row in recognition_rows)`` equals
	``total_transaction_price`` exactly at 2 decimals - rounding residuals are
	folded into the largest allocation / the final period of each obligation.
	"""

	total_transaction_price: float
	allocations: list[AllocationResult] = field(default_factory=list)
	recognition_rows: list[RecognitionRow] = field(default_factory=list)
	#: point-in-time obligations with no satisfied_date yet: their single
	#: recognition row carries no dates and must not be posted on a timetable.
	event_pending_keys: list[str] = field(default_factory=list)


@dataclass
class ModificationResult:
	"""Outcome of a contract modification (see :func:`engine.modification`).

	- ``plan`` is the new go-forward plan. For :data:`PROSPECTIVE` it covers
	  only the unrecognized remainder of the (new) transaction price; for
	  :data:`CUMULATIVE_CATCH_UP` it is the full re-derived plan.
	- ``catch_up_by_key`` is non-zero only for :data:`CUMULATIVE_CATCH_UP`:
	  per obligation key, ``new cumulative-to-date - recognized-to-date``
	  (negative when revenue must be clawed back).
	"""

	method: str
	plan: ContractPlan
	catch_up_by_key: dict[str, float] = field(default_factory=dict)
