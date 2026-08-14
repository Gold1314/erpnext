# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure ASC 606 / IFRS 15 allocation + recognition engine.

Zero frappe imports and no ambient time - every date the engine reasons about
is passed in. Adapters (the ``Revenue Contract`` doctype controller) translate
doctype rows into :mod:`erpnext.accounts.revenue.models` inputs and persist the
outputs.

Money handling: all outputs are rounded to 2 decimals half-up; rounding
residuals are folded into the largest allocation (:func:`allocate`) and into
the final period of each obligation (:func:`build_recognition_plan`) so sums
tie out exactly.
"""

from __future__ import annotations

import calendar
import datetime
from decimal import ROUND_HALF_UP, Decimal

from erpnext.accounts.revenue.models import (
	CUMULATIVE_CATCH_UP,
	MODIFICATION_METHODS,
	MONTHLY,
	OVER_TIME,
	PERIODICITIES,
	POINT_IN_TIME,
	AllocationResult,
	ContractPlan,
	ModificationResult,
	ObligationInput,
	RecognitionRow,
)


def round2(value: float) -> float:
	"""Round to 2 decimals, half away from zero (deterministic money rounding)."""
	return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def allocate(transaction_price: float, obligations: list[ObligationInput]) -> list[AllocationResult]:
	"""Step 4: allocate the transaction price by relative standalone selling price.

	``allocated_i = transaction_price * ssp_i / sum(ssp)``, rounded to 2
	decimals with the residual pushed into the largest allocation so that the
	allocations sum to ``transaction_price`` exactly.

	Obligations with ``ssp == 0`` get 0 - *unless every* obligation has
	``ssp == 0``, in which case the engine falls back to allocating by
	``stated_amount`` proportions (the contract's own stated prices are then
	the best available estimate of relative value; see DESIGN.md).
	"""
	if not obligations:
		return []

	weights = [max(o.ssp, 0.0) for o in obligations]
	if not any(weights):
		# zero-SSP fallback: stated-amount proportions
		weights = [max(o.stated_amount, 0.0) for o in obligations]

	total_weight = sum(weights)
	if not total_weight:
		if round2(transaction_price):
			raise ValueError(
				"Cannot allocate a non-zero transaction price: all obligations "
				"have zero SSP and zero stated amount."
			)
		return [AllocationResult(key=o.key, allocated_amount=0.0, allocation_pct=0.0) for o in obligations]

	allocations = [
		AllocationResult(
			key=obligation.key,
			allocated_amount=round2(transaction_price * weight / total_weight),
			allocation_pct=0.0,
		)
		for obligation, weight in zip(obligations, weights, strict=True)
	]

	# push the rounding residual into the largest allocation (first on ties)
	residual = round2(transaction_price - sum(a.allocated_amount for a in allocations))
	if residual:
		largest = max(allocations, key=lambda a: a.allocated_amount)
		largest.allocated_amount = round2(largest.allocated_amount + residual)

	for allocation in allocations:
		allocation.allocation_pct = (
			round(allocation.allocated_amount / transaction_price * 100.0, 4) if transaction_price else 0.0
		)

	return allocations


def _month_segments(
	start: datetime.date, end: datetime.date
) -> list[tuple[datetime.date, datetime.date, float]]:
	"""Split an inclusive date window into calendar-month segments.

	Returns ``(segment_start, segment_end, fraction_of_month)`` triples where
	the fraction is inclusive-days-covered / days-in-that-calendar-month - the
	same day-proration idea ``deferred_revenue.calculate_monthly_amount`` uses
	for partial first/last months.
	"""
	segments = []
	cursor = start
	while cursor <= end:
		days_in_month = calendar.monthrange(cursor.year, cursor.month)[1]
		month_end = datetime.date(cursor.year, cursor.month, days_in_month)
		segment_end = min(month_end, end)
		covered_days = (segment_end - cursor).days + 1
		segments.append((cursor, segment_end, covered_days / days_in_month))
		cursor = month_end + datetime.timedelta(days=1)
	return segments


def build_recognition_plan(
	transaction_price: float,
	obligations: list[ObligationInput],
	allocations: list[AllocationResult] | None = None,
	periodicity: str = MONTHLY,
) -> ContractPlan:
	"""Step 5: derive the per-period recognition plan from the allocation.

	- *Point in Time* obligations yield a single row dated on
	  ``satisfied_date``. When the satisfying event has not happened yet the
	  row carries ``None`` dates and the key is flagged in
	  ``event_pending_keys`` - such rows are excluded from any time-based
	  posting run until the obligation is marked satisfied.
	- *Over Time* obligations are recognized straight-line by calendar month
	  between ``start_date`` and ``end_date`` with day-proration for partial
	  first/last months; the rounding residual lands in the final period so
	  each obligation's rows total exactly its allocated amount.
	"""
	if periodicity not in PERIODICITIES:
		raise ValueError(f"Unsupported periodicity {periodicity!r}; supported: {PERIODICITIES}")

	if allocations is None:
		allocations = allocate(transaction_price, obligations)

	allocated_by_key = {a.key: a.allocated_amount for a in allocations}
	rows: list[RecognitionRow] = []
	event_pending_keys: list[str] = []

	for obligation in obligations:
		allocated = allocated_by_key.get(obligation.key, 0.0)

		if obligation.satisfaction_method == POINT_IN_TIME:
			rows.append(
				RecognitionRow(
					key=obligation.key,
					period_start=obligation.satisfied_date,
					period_end=obligation.satisfied_date,
					amount=allocated,
				)
			)
			if not obligation.satisfied_date:
				event_pending_keys.append(obligation.key)

		elif obligation.satisfaction_method == OVER_TIME:
			if not (obligation.start_date and obligation.end_date):
				raise ValueError(f"Over Time obligation {obligation.key!r} needs start_date and end_date.")
			if obligation.start_date > obligation.end_date:
				raise ValueError(f"Obligation {obligation.key!r}: start_date is after end_date.")

			segments = _month_segments(obligation.start_date, obligation.end_date)
			total_fraction = sum(fraction for _, _, fraction in segments)
			booked = 0.0
			for index, (segment_start, segment_end, fraction) in enumerate(segments):
				if index < len(segments) - 1:
					amount = round2(allocated * fraction / total_fraction)
					booked = round2(booked + amount)
				else:
					amount = round2(allocated - booked)  # residual into the final period
				rows.append(
					RecognitionRow(
						key=obligation.key,
						period_start=segment_start,
						period_end=segment_end,
						amount=amount,
					)
				)

		else:
			raise ValueError(
				f"Obligation {obligation.key!r}: unknown satisfaction method "
				f"{obligation.satisfaction_method!r}."
			)

	return ContractPlan(
		total_transaction_price=round2(transaction_price),
		allocations=allocations,
		recognition_rows=rows,
		event_pending_keys=event_pending_keys,
	)


def modification(
	original_plan: ContractPlan,
	recognized_to_date_by_key: dict[str, float],
	new_transaction_price: float,
	new_obligations: list[ObligationInput],
	method: str,
	as_of: datetime.date | None = None,
	periodicity: str = MONTHLY,
) -> ModificationResult:
	"""Handle a contract modification (ASC 606-10-25-10 ff., simplified v1).

	*Prospective* (treat as termination of the old + creation of a new
	contract, ASC 606-10-25-13a): only the unrecognized remainder
	(``new_transaction_price - total recognized to date``) is reallocated
	across ``new_obligations``. The caller passes obligations describing the
	*remaining* performance (service windows starting at the modification
	date); revenue already recognized is never restated, so
	``catch_up_by_key`` is empty.

	*Cumulative Catch-up* (modification is part of the original single
	performance obligation stream, ASC 606-10-25-13b): the full new
	transaction price is reallocated and a full plan re-derived. For each
	obligation key, ``catch_up_by_key`` holds ``new cumulative-to-date (rows
	with period_end <= as_of) - recognized-to-date`` - the one-time adjustment
	to post at the modification date (negative = revenue clawback). ``as_of``
	is required for this method (no ambient time in the engine).

	``original_plan`` is accepted for API completeness/audit context; v1's
	math only needs the recognized-to-date amounts (see DESIGN.md for the
	documented simplifications vs. full contract-combination rules).
	"""
	if method not in MODIFICATION_METHODS:
		raise ValueError(f"Unknown modification method {method!r}; supported: {MODIFICATION_METHODS}")

	recognized_total = round2(sum(recognized_to_date_by_key.values()))

	if method == CUMULATIVE_CATCH_UP:
		if as_of is None:
			raise ValueError("Cumulative Catch-up requires as_of (the modification date).")

		plan = build_recognition_plan(new_transaction_price, new_obligations, periodicity=periodicity)

		cumulative_by_key: dict[str, float] = {}
		for row in plan.recognition_rows:
			if row.period_end and row.period_end <= as_of:
				cumulative_by_key[row.key] = round2(cumulative_by_key.get(row.key, 0.0) + row.amount)

		keys = {a.key for a in plan.allocations} | set(recognized_to_date_by_key)
		catch_up_by_key = {
			key: round2(cumulative_by_key.get(key, 0.0) - recognized_to_date_by_key.get(key, 0.0))
			for key in sorted(keys)
		}
		return ModificationResult(method=method, plan=plan, catch_up_by_key=catch_up_by_key)

	# Prospective
	remaining_price = round2(new_transaction_price - recognized_total)
	if remaining_price < 0:
		raise ValueError(
			"Prospective modification: recognized-to-date exceeds the new "
			"transaction price; use Cumulative Catch-up to claw back revenue."
		)
	plan = build_recognition_plan(remaining_price, new_obligations, periodicity=periodicity)
	return ModificationResult(method=method, plan=plan, catch_up_by_key={})
