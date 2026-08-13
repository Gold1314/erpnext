# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure lease accounting engine (lessee side, ASC 842 / IFRS 16).

No frappe imports, no ambient time - every function takes explicit dates.
Conventions (documented in detail in DESIGN.md next to this file):

- MONTHLY compounding: the periodic rate is ``annual_rate_pct / 12 / 100``.
- Discount exponents are whole months between commencement and due date:
  floor months for "Beginning of Period" timing (a payment due at
  commencement is undiscounted), ceiling months for "End of Period" timing
  (a payment due on the last day of the first month is discounted one
  period, matching an ordinary annuity).
- All monetary outputs are rounded to 2 decimals (ROUND_HALF_UP); rounding
  residuals are pushed into the final schedule row so that both the closing
  liability and the closing ROU land on exactly 0.00.
"""

from __future__ import annotations

import calendar
import datetime
from decimal import ROUND_HALF_UP, Decimal

from erpnext.assets.leasing.models import (
	FREQUENCY_MONTHS,
	SHORT_TERM_THRESHOLD_MONTHS,
	TIMING_BEGINNING,
	TIMING_END,
	AmortizationRow,
	LeasePayment,
	LeaseSchedule,
	LeaseTerms,
	RemeasurementResult,
)

ONE_DAY = datetime.timedelta(days=1)


def round2(value: float) -> float:
	"""Round to 2 decimals, half away from zero, deterministically."""
	return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def add_months(date: datetime.date, months: int) -> datetime.date:
	"""Calendar-month arithmetic, clamping the day to the target month's end."""
	month_index = date.month - 1 + months
	year = date.year + month_index // 12
	month = month_index % 12 + 1
	day = min(date.day, calendar.monthrange(year, month)[1])
	return datetime.date(year, month, day)


def whole_months(start: datetime.date, end: datetime.date) -> int:
	"""Floor count of whole calendar months from ``start`` to ``end``."""
	months = (end.year - start.year) * 12 + (end.month - start.month)
	if end.day < start.day:
		months -= 1
	return months


def ceil_months(start: datetime.date, end: datetime.date) -> int:
	"""Ceiling count of months: floor months, +1 unless ``end`` is exactly on a boundary."""
	months = whole_months(start, end)
	if add_months(start, months) == end:
		return months
	return months + 1


def term_months(commencement_date: datetime.date, end_date: datetime.date) -> int:
	"""Number of monthly periods needed to cover [commencement, end] inclusive."""
	if end_date < commencement_date:
		raise ValueError("end_date cannot be before commencement_date")
	months = whole_months(commencement_date, end_date)
	if add_months(commencement_date, months) <= end_date:
		months += 1
	return max(months, 1)


def classify_short_term(terms: LeaseTerms, threshold_months: int = SHORT_TERM_THRESHOLD_MONTHS) -> bool:
	"""True when the lease term is at or below the short-term threshold."""
	return term_months(terms.commencement_date, terms.end_date) <= threshold_months


def _validate_timing(timing: str) -> None:
	if timing not in (TIMING_END, TIMING_BEGINNING):
		raise ValueError(f"Unknown payment timing: {timing!r}")


def _discount_exponent(commencement_date: datetime.date, due_date: datetime.date, timing: str) -> int:
	if due_date < commencement_date:
		raise ValueError(f"Payment due date {due_date} is before commencement {commencement_date}")
	if timing == TIMING_BEGINNING:
		return whole_months(commencement_date, due_date)
	return ceil_months(commencement_date, due_date)


def present_value(
	payments: list[LeasePayment],
	annual_rate_pct: float,
	commencement_date: datetime.date,
	timing: str = TIMING_END,
) -> float:
	"""PV of the payments at commencement under the monthly-compounding convention."""
	_validate_timing(timing)
	rate = annual_rate_pct / 12.0 / 100.0
	total = 0.0
	for payment in payments:
		exponent = _discount_exponent(commencement_date, payment.due_date, timing)
		if rate:
			total += payment.amount / ((1.0 + rate) ** exponent)
		else:
			total += payment.amount
	return round2(total)


def generate_payment_rows(
	commencement_date: datetime.date,
	end_date: datetime.date,
	frequency: str,
	amount: float,
	timing: str = TIMING_END,
) -> list[LeasePayment]:
	"""Convenience generator of level payments over the term.

	"Beginning of Period" payments fall on period starts (commencement,
	then every ``frequency`` months); "End of Period" payments fall on the
	last day of each frequency period. The child table on the doctype is the
	source of truth - this only pre-fills it.
	"""
	_validate_timing(timing)
	step = FREQUENCY_MONTHS.get(frequency)
	if not step:
		raise ValueError(f"Unknown payment frequency: {frequency!r}")
	if amount <= 0:
		raise ValueError("Payment amount must be positive")

	months = term_months(commencement_date, end_date)
	payments = []
	if timing == TIMING_BEGINNING:
		tick = 0
		while tick < months:
			payments.append(LeasePayment(due_date=add_months(commencement_date, tick), amount=amount))
			tick += step
	else:
		tick = step
		while tick <= months:
			due = min(add_months(commencement_date, tick) - ONE_DAY, end_date)
			payments.append(LeasePayment(due_date=due, amount=amount))
			tick += step
	return payments


def _validate_terms(terms: LeaseTerms) -> None:
	_validate_timing(terms.payment_timing)
	if terms.end_date <= terms.commencement_date:
		raise ValueError("end_date must be after commencement_date")
	if not terms.payments:
		raise ValueError("At least one lease payment is required")
	for payment in terms.payments:
		if payment.amount <= 0:
			raise ValueError(f"Payment amount must be positive (got {payment.amount})")
		if payment.due_date < terms.commencement_date or payment.due_date > terms.end_date:
			raise ValueError(
				f"Payment due {payment.due_date} falls outside the lease term "
				f"[{terms.commencement_date} .. {terms.end_date}]"
			)


def build_schedule(terms: LeaseTerms, rou_useful_life_months: int | None = None) -> LeaseSchedule:
	"""Full monthly liability + ROU amortization schedule.

	- ``initial_liability = initial_rou = PV`` of the payments (v1: no
	  initial direct costs / incentives - see DESIGN.md).
	- Each payment is anchored to its month tick (the discount exponent) so
	  the roll-forward is exactly consistent with the PV; the final row
	  absorbs all rounding residuals and closes liability and ROU to 0.00.
	- ROU depreciates straight-line over ``min(term, rou_useful_life_months)``
	  months (v1 default: the lease term).
	"""
	_validate_terms(terms)

	rate = terms.annual_discount_rate_pct / 12.0 / 100.0
	months = term_months(terms.commencement_date, terms.end_date)
	initial_liability = present_value(
		terms.payments, terms.annual_discount_rate_pct, terms.commencement_date, terms.payment_timing
	)
	initial_rou = initial_liability

	# payments applied at the start of period i (index i-1..) vs at its end
	start_payments = {}  # period index -> amount
	end_payments = {}
	for payment in terms.payments:
		tick = _discount_exponent(terms.commencement_date, payment.due_date, terms.payment_timing)
		if terms.payment_timing == TIMING_BEGINNING:
			if tick >= months:  # clamp: due at the very end of the term
				end_payments[months] = end_payments.get(months, 0.0) + payment.amount
			else:
				start_payments[tick + 1] = start_payments.get(tick + 1, 0.0) + payment.amount
		else:
			if tick == 0:  # clamp: due exactly at commencement
				start_payments[1] = start_payments.get(1, 0.0) + payment.amount
			else:
				period = min(tick, months)
				end_payments[period] = end_payments.get(period, 0.0) + payment.amount

	depreciation_months = months
	if rou_useful_life_months:
		depreciation_months = min(months, int(rou_useful_life_months))
	monthly_depreciation = round2(initial_rou / depreciation_months) if depreciation_months else 0.0

	rows = []
	opening_liability = initial_liability
	opening_rou = initial_rou

	for index in range(1, months + 1):
		period_start = add_months(terms.commencement_date, index - 1)
		period_end = min(add_months(terms.commencement_date, index) - ONE_DAY, terms.end_date)

		paid_at_start = round2(start_payments.get(index, 0.0))
		paid_at_end = round2(end_payments.get(index, 0.0))
		payment = round2(paid_at_start + paid_at_end)

		interest_base = max(opening_liability - paid_at_start, 0.0)
		interest = round2(interest_base * rate) if rate else 0.0

		if index == months:
			# final row: force the liability to close at exactly 0.00 by
			# absorbing any rounding residual into interest
			interest = round2(payment - opening_liability)
			principal = round2(opening_liability)
			closing_liability = 0.0
		else:
			closing_liability = round2(opening_liability + interest - payment)
			principal = round2(payment - interest)

		if index < depreciation_months:
			rou_depreciation = monthly_depreciation
			closing_rou = round2(opening_rou - rou_depreciation)
		elif index == depreciation_months:
			# final depreciation row absorbs the ROU rounding residual
			rou_depreciation = round2(opening_rou)
			closing_rou = 0.0
		else:
			rou_depreciation = 0.0
			closing_rou = 0.0

		rows.append(
			AmortizationRow(
				period_start=period_start,
				period_end=period_end,
				opening_liability=opening_liability,
				interest=interest,
				payment=payment,
				principal=principal,
				closing_liability=closing_liability,
				opening_rou=opening_rou,
				rou_depreciation=rou_depreciation,
				closing_rou=closing_rou,
			)
		)
		opening_liability = closing_liability
		opening_rou = closing_rou

	return LeaseSchedule(
		initial_liability=initial_liability,
		initial_rou=initial_rou,
		rows=rows,
		total_interest=round2(sum(row.interest for row in rows)),
		total_payments=round2(sum(row.payment for row in rows)),
	)


def remeasure(
	schedule: LeaseSchedule,
	effective_date: datetime.date,
	new_terms: LeaseTerms,
) -> RemeasurementResult:
	"""v1 remeasurement: fresh schedule for the remaining payments at the new rate.

	``new_terms`` must describe the lease *from* ``effective_date`` (its
	``commencement_date`` is the effective date, its payments are the
	remaining ones). The carrying liability is read off the old schedule as
	the closing liability of the last period fully elapsed before the
	effective date. The returned ``rou_adjustment`` is the amount by which
	both the lease liability and the ROU asset are adjusted (ASC 842
	simplifications documented in DESIGN.md).
	"""
	if new_terms.commencement_date != effective_date:
		raise ValueError("new_terms.commencement_date must equal effective_date")

	carrying_liability = schedule.initial_liability
	for row in schedule.rows:
		if row.period_end < effective_date:
			carrying_liability = row.closing_liability

	new_schedule = build_schedule(new_terms)
	rou_adjustment = round2(new_schedule.initial_liability - carrying_liability)
	return RemeasurementResult(
		new_schedule=new_schedule,
		carrying_liability=carrying_liability,
		rou_adjustment=rou_adjustment,
	)
