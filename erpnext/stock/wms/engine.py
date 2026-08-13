# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure WMS-lite engine: directed putaway, directed picking, ABC classification
and cycle-count due-date math.

Consumes plain :class:`LocationInfo` rows (built by ``loaders.py``) and returns
plain suggestion dataclasses. No frappe imports and no ambient "now" - callers
pass ``as_of`` where a date matters. Semantics are documented in ``DESIGN.md``.

Putaway preference order (:data:`PUTAWAY_TYPE_RANK`): dedicated storage first
(Bin, then Pick Face, then Bulk), structural leaves next, Staging and QC last -
and *within* a rank, lower ``pick_sequence`` first. Locations with capacity 0
(unlimited) are only used once every finite-capacity location is full: a real
bin with a qty budget is always a better directed answer than a catch-all.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping

from erpnext.stock.wms.models import (
	AISLE,
	BIN,
	BULK,
	CLASS_A,
	CLASS_B,
	CLASS_C,
	PICK_FACE,
	QC,
	RACK,
	STAGING,
	ZONE,
	LocationInfo,
	PickSuggestion,
	PutawaySuggestion,
)

#: float-comparison tolerance for quantities
EPSILON = 1e-9

#: lower rank = preferred for putaway; unknown types fall back to the
#: structural rank (3)
PUTAWAY_TYPE_RANK = {
	BIN: 0,
	PICK_FACE: 1,
	BULK: 2,
	RACK: 3,
	AISLE: 3,
	ZONE: 3,
	STAGING: 4,
	QC: 5,
}
_DEFAULT_TYPE_RANK = 3

#: default ABC cumulative-value-share boundaries
DEFAULT_A_PCT = 0.8
DEFAULT_B_PCT = 0.95


def suggest_putaway(
	locations: Iterable[LocationInfo], incoming_qty: float
) -> tuple[list[PutawaySuggestion], float]:
	"""Fill ``incoming_qty`` into locations, honoring remaining capacity.

	Ordering: finite-capacity locations first (unlimited ones are a last
	resort), then by :data:`PUTAWAY_TYPE_RANK` (Bin > Pick Face > Bulk >
	structural > Staging > QC), then lower ``pick_sequence``, then ``code``
	for determinism. A finite location contributes at most
	``capacity_qty - current_qty``; a full one is skipped. An unlimited
	location absorbs everything left.

	Returns ``(suggestions, unallocated_remainder)`` - the remainder is
	non-zero only when every location is finite and full.
	"""
	remaining = float(incoming_qty)
	if remaining <= EPSILON:
		return [], 0.0

	def sort_key(loc: LocationInfo):
		return (
			0 if float(loc.capacity_qty) > 0 else 1,  # unlimited = last resort
			PUTAWAY_TYPE_RANK.get(loc.location_type, _DEFAULT_TYPE_RANK),
			int(loc.pick_sequence or 0),
			loc.code,
		)

	suggestions: list[PutawaySuggestion] = []
	for loc in sorted(locations, key=sort_key):
		if remaining <= EPSILON:
			break

		if float(loc.capacity_qty) > 0:
			free = float(loc.capacity_qty) - float(loc.current_qty)
			if free <= EPSILON:
				continue
			take = min(free, remaining)
		else:
			take = remaining

		suggestions.append(PutawaySuggestion(location_code=loc.code, qty=take))
		remaining -= take

	return suggestions, (remaining if remaining > EPSILON else 0.0)


def suggest_picks(
	locations_with_stock: Iterable[LocationInfo], required_qty: float
) -> tuple[list[PickSuggestion], float]:
	"""Consume ``required_qty`` walking the pick path.

	``locations_with_stock`` carry the *item's* qty in ``current_qty``.
	Ordering: lower ``pick_sequence`` first (the physical pick path), then
	largest ``current_qty`` (fewer visits when sequences tie), then ``code``
	for determinism. Empty locations are skipped.

	Returns ``(suggestions, shortfall)``.
	"""
	remaining = float(required_qty)
	if remaining <= EPSILON:
		return [], 0.0

	candidates = sorted(
		(loc for loc in locations_with_stock if float(loc.current_qty) > EPSILON),
		key=lambda loc: (int(loc.pick_sequence or 0), -float(loc.current_qty), loc.code),
	)

	suggestions: list[PickSuggestion] = []
	for loc in candidates:
		if remaining <= EPSILON:
			break

		take = min(float(loc.current_qty), remaining)
		suggestions.append(
			PickSuggestion(location_code=loc.code, qty=take, pick_sequence=int(loc.pick_sequence or 0))
		)
		remaining -= take

	return suggestions, (remaining if remaining > EPSILON else 0.0)


def classify_abc(
	items_with_velocity: Mapping[str, float],
	a_pct: float = DEFAULT_A_PCT,
	b_pct: float = DEFAULT_B_PCT,
) -> dict[str, str]:
	"""Classic cumulative-velocity ABC split by consumption-value share.

	Items are ranked by velocity (consumption value) descending, ties broken
	by item code for determinism. An item's class is decided by the
	cumulative share *before* it: still below ``a_pct`` -> "A", below
	``b_pct`` -> "B", else "C". (The "before" convention keeps the top item
	"A" even when it alone exceeds ``a_pct``.) Items with zero or negative
	velocity are always "C"; when total velocity is zero everything is "C".
	"""
	if not 0 < a_pct <= b_pct <= 1:
		raise ValueError(f"Expected 0 < a_pct <= b_pct <= 1, got a_pct={a_pct}, b_pct={b_pct}")

	ranked = sorted(items_with_velocity.items(), key=lambda pair: (-float(pair[1]), pair[0]))
	total = sum(float(value) for _, value in ranked if float(value) > 0)

	classes: dict[str, str] = {}
	if total <= EPSILON:
		return {item: CLASS_C for item, _ in ranked}

	cumulative = 0.0
	for item, value in ranked:
		value = float(value)
		if value <= 0:
			classes[item] = CLASS_C
			continue

		share_before = cumulative / total
		if share_before < a_pct - EPSILON:
			classes[item] = CLASS_A
		elif share_before < b_pct - EPSILON:
			classes[item] = CLASS_B
		else:
			classes[item] = CLASS_C

		cumulative += value

	return classes


def next_count_due(
	last_counted: datetime.date | None,
	frequency_days: int,
	as_of: datetime.date,
) -> datetime.date:
	"""Next cycle-count due date.

	A never-counted item is due immediately (``as_of``); otherwise the count
	is due ``frequency_days`` after the last count.
	"""
	if int(frequency_days) <= 0:
		raise ValueError(f"frequency_days must be positive, got {frequency_days}")

	if last_counted is None:
		return as_of

	return last_counted + datetime.timedelta(days=int(frequency_days))


def is_count_due(
	last_counted: datetime.date | None,
	frequency_days: int,
	as_of: datetime.date,
) -> bool:
	"""True when the next due date is on or before ``as_of``."""
	return next_count_due(last_counted, frequency_days, as_of) <= as_of
