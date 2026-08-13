# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure cash-flow projection engine.

Consumes plain :class:`CashFlowItem` rows (built by ``loaders.py``), applies a
:class:`ForecastScenario`, buckets everything into Daily/Weekly/Monthly
periods and returns a :class:`ForecastResult` with running balances.

No frappe imports and no ambient "now" - callers pass ``start``/``end``.
"""

from __future__ import annotations

import calendar
import datetime

from erpnext.accounts.forecasting.models import (
	DAILY,
	MONTHLY,
	PAYABLE,
	PERIODICITIES,
	PIPELINE_SOURCES,
	PURCHASE_ORDER,
	RECEIVABLE,
	SALES_ORDER,
	WEEKLY,
	CashFlowItem,
	ForecastPeriod,
	ForecastResult,
	ForecastScenario,
)


def build_forecast(
	items: list[CashFlowItem],
	opening_balance: float,
	start: datetime.date,
	end: datetime.date,
	periodicity: str = WEEKLY,
	scenario: ForecastScenario | None = None,
) -> ForecastResult:
	"""Project ``items`` over ``[start, end]`` (both inclusive).

	- Scenario shifts/haircuts/toggles are applied first (see
	  :func:`apply_scenario`).
	- Items dated before ``start`` (overdue receivables/payables) roll into
	  the first period - they are expected to settle soon.
	- Items dated after ``end`` are dropped.
	- ``closing_balance`` runs cumulatively from ``opening_balance``.
	"""
	if end < start:
		raise ValueError(f"end {end} is before start {start}")
	if periodicity not in PERIODICITIES:
		raise ValueError(f"unsupported periodicity {periodicity!r}, expected one of {PERIODICITIES}")

	scenario = scenario or ForecastScenario()
	periods = build_periods(start, end, periodicity)

	for item in apply_scenario(items, scenario):
		period = find_period(periods, item.posting_date)
		if period is not None:
			period.add(item.source_type, item.amount)

	balance = opening_balance
	for period in periods:
		period.opening_balance = balance
		balance += period.net
		period.closing_balance = balance

	return ForecastResult(periods=periods, opening_balance=opening_balance, periodicity=periodicity)


def apply_scenario(items: list[CashFlowItem], scenario: ForecastScenario) -> list[CashFlowItem]:
	"""Return new items with scenario toggles, delays and haircuts applied.

	The input list is never mutated - shifted/scaled items are copies.
	"""
	haircut_factor = max(0.0, 1.0 - scenario.confidence_haircut_pct / 100.0)
	adjusted = []

	for item in items:
		if item.source_type == SALES_ORDER and not scenario.include_sales_orders:
			continue
		if item.source_type == PURCHASE_ORDER and not scenario.include_purchase_orders:
			continue

		shift_days = 0
		if item.source_type == RECEIVABLE:
			shift_days = scenario.receivable_delay_days
		elif item.source_type == PAYABLE:
			shift_days = scenario.payable_delay_days

		amount = item.amount
		if item.source_type in PIPELINE_SOURCES:
			amount *= haircut_factor

		if shift_days or amount != item.amount:
			item = CashFlowItem(
				posting_date=item.posting_date + datetime.timedelta(days=shift_days),
				amount=amount,
				source_type=item.source_type,
				party=item.party,
				party_type=item.party_type,
				reference_doctype=item.reference_doctype,
				reference_name=item.reference_name,
				bank_account=item.bank_account,
				currency=item.currency,
			)

		adjusted.append(item)

	return adjusted


def build_periods(start: datetime.date, end: datetime.date, periodicity: str) -> list[ForecastPeriod]:
	"""Contiguous inclusive buckets covering ``[start, end]``.

	Daily: one bucket per day. Weekly: 7-day buckets anchored on ``start``.
	Monthly: first bucket runs to the end of ``start``'s calendar month, then
	whole calendar months; the last bucket is clipped to ``end``.
	"""
	periods = []
	cursor = start
	index = 1

	while cursor <= end:
		if periodicity == DAILY:
			period_end = cursor
			label = cursor.isoformat()
		elif periodicity == WEEKLY:
			period_end = min(cursor + datetime.timedelta(days=6), end)
			label = f"{cursor.isoformat()} - {period_end.isoformat()}"
		else:  # MONTHLY
			period_end = min(end_of_month(cursor), end)
			label = f"{calendar.month_abbr[cursor.month]} {cursor.year}"

		periods.append(
			ForecastPeriod(key=f"period_{index}", label=label, from_date=cursor, to_date=period_end)
		)
		cursor = period_end + datetime.timedelta(days=1)
		index += 1

	return periods


def find_period(periods: list[ForecastPeriod], posting_date: datetime.date) -> ForecastPeriod | None:
	"""Bucket for ``posting_date``; before-start rolls into the first bucket
	(overdue), after-end returns None (out of horizon)."""
	if not periods:
		return None
	if posting_date < periods[0].from_date:
		return periods[0]
	for period in periods:
		if period.from_date <= posting_date <= period.to_date:
			return period
	return None


def end_of_month(day: datetime.date) -> datetime.date:
	return day.replace(day=calendar.monthrange(day.year, day.month)[1])
