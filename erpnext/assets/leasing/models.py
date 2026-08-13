# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for lessee lease accounting (ASC 842 / IFRS 16).

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (see erpnext/manufacturing/scheduling and
erpnext/accounts/forecasting for the pattern this module follows).

All monetary amounts are floats rounded to 2 decimal places at the edges by
the engine (see ``engine.round2``); dates are ``datetime.date``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Payment timing conventions
TIMING_END = "End of Period"
TIMING_BEGINNING = "Beginning of Period"
PAYMENT_TIMINGS = (TIMING_END, TIMING_BEGINNING)

# Payment frequencies (step expressed in months)
FREQUENCY_MONTHS = {
	"Monthly": 1,
	"Quarterly": 3,
	"Annually": 12,
}

#: term length at or below which a lease qualifies for the short-term
#: recognition exemption (ASC 842-20-25-2 / IFRS 16.5)
SHORT_TERM_THRESHOLD_MONTHS = 12


@dataclass
class LeasePayment:
	"""One contractual lease payment."""

	due_date: datetime.date
	amount: float


@dataclass
class LeaseTerms:
	"""Everything the engine needs to know about a lease contract.

	``payments`` is the source of truth - the engine never generates payments
	implicitly from a frequency (that is a separate convenience helper).
	"""

	commencement_date: datetime.date
	end_date: datetime.date
	payments: list[LeasePayment] = field(default_factory=list)
	annual_discount_rate_pct: float = 0.0
	payment_timing: str = TIMING_END


@dataclass
class AmortizationRow:
	"""One monthly period of the combined liability + ROU roll-forward."""

	period_start: datetime.date
	period_end: datetime.date
	opening_liability: float
	interest: float
	payment: float
	principal: float
	closing_liability: float
	opening_rou: float
	rou_depreciation: float
	closing_rou: float


@dataclass
class LeaseSchedule:
	"""Complete amortization schedule for one lease."""

	initial_liability: float
	initial_rou: float
	rows: list[AmortizationRow] = field(default_factory=list)
	total_interest: float = 0.0
	total_payments: float = 0.0


@dataclass
class RemeasurementResult:
	"""Outcome of remeasuring a lease at an effective date.

	``rou_adjustment`` is the signed delta between the new present value and
	the carrying liability immediately before the effective date: positive
	means both liability and ROU increase, negative means they decrease.
	"""

	new_schedule: LeaseSchedule
	carrying_liability: float
	rou_adjustment: float
