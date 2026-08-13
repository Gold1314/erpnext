# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure ATP (available-to-promise) netting engine.

Consumes plain :class:`SupplyEvent` / :class:`DemandEvent` rows (built by
``loaders.py``) and answers "what is the earliest date qty N of this item is
available, after everything already committed?".

Semantics (documented in full in ``DESIGN.md``):

1. Existing demand is netted first: demand rows, in chronological
   ``required_date`` order, greedily consume the earliest-dated supply. What
   survives is the *uncommitted* supply timeline.
2. The new request is then allocated against that remaining timeline,
   earliest supply first. The promised date is the ``available_date`` of the
   last supply slice needed (never before ``today``, never before the
   requested date when one is given).
3. Supply dated past ``horizon_end`` is ignored; if the remaining supply
   inside the horizon never reaches the requested qty the request is not
   fulfillable and ``shortfall`` reports the gap.

No frappe imports and no ambient "now" - callers pass ``today``.
"""

from __future__ import annotations

import datetime

from erpnext.stock.promising.models import (
	Allocation,
	DemandEvent,
	PromiseRequest,
	PromiseResult,
	SupplyEvent,
)

#: float-comparison tolerance for quantities
EPSILON = 1e-9

DEFAULT_HORIZON_DAYS = 90


def promise(
	request: PromiseRequest,
	supply_events: list[SupplyEvent],
	demand_events: list[DemandEvent],
	horizon_end: datetime.date,
	today: datetime.date,
) -> PromiseResult:
	"""Evaluate one request against a supply/demand picture.

	``supply_events`` and ``demand_events`` must all belong to the request's
	(item, warehouse); the engine does not key by item. Existing demand is
	committed first (see :func:`net_supply_against_demand`), then the request
	is allocated from what remains.
	"""
	result = _guard_request(request)
	if result:
		return result

	remaining = net_supply_against_demand(supply_events, demand_events, horizon_end, today)
	return _allocate(request, remaining, horizon_end, today)


def promise_many(
	requests: list[PromiseRequest],
	supply_by_key: dict[tuple[str, str], list[SupplyEvent]],
	demand_by_key: dict[tuple[str, str], list[DemandEvent]],
	horizon_end: datetime.date,
	today: datetime.date,
) -> list[PromiseResult]:
	"""Evaluate several requests, allocating sequentially in list order.

	``supply_by_key`` / ``demand_by_key`` are keyed by ``(item_code,
	warehouse)``. Each pool is netted against its committed demand once;
	every fulfilled request then consumes from the shared remaining pool, so
	two lines of the same item on one Sales Order never double-count the
	same supply.
	"""
	remaining_by_key: dict[tuple[str, str], list[SupplyEvent]] = {}
	results = []

	for request in requests:
		guard = _guard_request(request)
		if guard:
			results.append(guard)
			continue

		key = (request.item_code, request.warehouse)
		if key not in remaining_by_key:
			remaining_by_key[key] = net_supply_against_demand(
				supply_by_key.get(key, []), demand_by_key.get(key, []), horizon_end, today
			)

		result = _allocate(request, remaining_by_key[key], horizon_end, today)
		if result.fulfillable:
			_consume(remaining_by_key[key], result.allocation)

		results.append(result)

	return results


def net_supply_against_demand(
	supply_events: list[SupplyEvent],
	demand_events: list[DemandEvent],
	horizon_end: datetime.date,
	today: datetime.date,
) -> list[SupplyEvent]:
	"""Commit existing demand against supply; return the uncommitted remainder.

	- Supply dated after ``horizon_end`` is dropped; supply dated before
	  ``today`` (overdue POs, late WOs) is treated as arriving ``today``.
	- Demand rows are processed in ``required_date`` order and each consumes
	  the earliest-dated remaining supply - even supply arriving after its
	  required date (the commitment exists whether or not it will be met on
	  time), so a later request can never borrow units an earlier commitment
	  will take.
	- Demand is **not** horizon-filtered: a commitment beyond the horizon
	  still consumes supply (conservative, never double-promises).
	"""
	remaining = [
		SupplyEvent(
			available_date=max(event.available_date, today),
			qty=event.qty,
			source_type=event.source_type,
			reference=event.reference,
		)
		for event in supply_events
		if event.qty > EPSILON and event.available_date <= horizon_end
	]
	remaining.sort(key=lambda event: event.available_date)

	for demand in sorted(demand_events, key=lambda event: event.required_date):
		open_qty = demand.qty
		if open_qty <= EPSILON:
			continue

		for event in remaining:
			if event.qty <= EPSILON:
				continue

			consumed = min(event.qty, open_qty)
			event.qty -= consumed
			open_qty -= consumed
			if open_qty <= EPSILON:
				break

	return [event for event in remaining if event.qty > EPSILON]


def _guard_request(request: PromiseRequest) -> PromiseResult | None:
	if request.qty > EPSILON:
		return None

	return PromiseResult(
		item_code=request.item_code,
		qty=request.qty,
		promised_date=None,
		fulfillable=False,
		shortfall=0.0,
		allocation=[],
		message="Requested quantity must be greater than zero",
	)


def _allocate(
	request: PromiseRequest,
	remaining: list[SupplyEvent],
	horizon_end: datetime.date,
	today: datetime.date,
) -> PromiseResult:
	allocation: list[Allocation] = []
	allocated = 0.0

	for event in sorted(remaining, key=lambda event: event.available_date):
		if event.qty <= EPSILON:
			continue

		take = min(event.qty, request.qty - allocated)
		allocation.append(Allocation(event.source_type, event.reference, event.available_date, take))
		allocated += take
		if allocated >= request.qty - EPSILON:
			break

	if allocated < request.qty - EPSILON:
		horizon_days = (horizon_end - today).days
		return PromiseResult(
			item_code=request.item_code,
			qty=request.qty,
			promised_date=None,
			fulfillable=False,
			shortfall=request.qty - allocated,
			allocation=allocation,
			message=(
				f"Insufficient planned supply within {horizon_days}-day horizon: "
				f"{allocated:g} of {request.qty:g} available"
			),
		)

	availability_date = max(part.available_date for part in allocation)
	promised_date = max(availability_date, today)
	if request.requested_date:
		promised_date = max(promised_date, request.requested_date)

	message = f"Available from {availability_date.isoformat()}"
	if request.requested_date and availability_date > request.requested_date:
		message = (
			f"Requested {request.requested_date.isoformat()}, "
			f"earliest availability {availability_date.isoformat()}"
		)

	return PromiseResult(
		item_code=request.item_code,
		qty=request.qty,
		promised_date=promised_date,
		fulfillable=True,
		shortfall=0.0,
		allocation=allocation,
		message=message,
	)


def _consume(remaining: list[SupplyEvent], allocation: list[Allocation]) -> None:
	"""Subtract a fulfilled request's allocation from the remaining pool."""
	for part in allocation:
		open_qty = part.qty
		for event in remaining:
			if event.qty <= EPSILON or event.available_date != part.available_date:
				continue
			if event.source_type != part.source_type or event.reference != part.reference:
				continue

			consumed = min(event.qty, open_qty)
			event.qty -= consumed
			open_qty -= consumed
			if open_qty <= EPSILON:
				break
