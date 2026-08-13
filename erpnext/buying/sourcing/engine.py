# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure sourcing engine: qualification questionnaire scoring, document-expiry
classification, weighted bid scoring and award scenario building.

No frappe imports and no ambient "now" - callers pass ``as_of`` where a date
matters (the doctype controllers pass ``frappe.utils.nowdate()``). Semantics
are documented in ``DESIGN.md``.

The weighted approach mirrors the Supplier Scorecard engine
(``supplier_scorecard.py``: weighted criteria normalized to a 0-100
``supplier_score``), so a scorecard standing can be dropped straight into the
bid-scoring weights without rescaling.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence

from erpnext.buying.sourcing.models import (
	BLOCKING_DOCUMENT_STATUSES,
	DEFAULT_RISK_BANDS,
	DEFAULT_WEIGHTS,
	EXPIRED,
	EXPIRING,
	MISSING,
	RISK_HIGH,
	SCENARIO_BEST_WEIGHTED,
	SCENARIO_LOWEST_PRICE,
	SCENARIO_SINGLE_SUPPLIER,
	VALID,
	W_LEAD_TIME,
	W_PRICE,
	W_QUALIFICATION,
	W_SCORECARD,
	AwardScenario,
	AwardScore,
	BidLine,
	DocumentRequirement,
	DocumentStatus,
	QualificationResult,
	QuestionnaireAnswer,
)

#: float-comparison tolerance
EPSILON = 1e-9

#: days added to both sides of the lead-time ratio so that a 0-day lead time
#: never divides by zero and never zeroes out every competing supplier.
#: With the floor, all-zero lead times score 100 each (1/1) and a 0-vs-2 day
#: race scores 100 vs 33.3 instead of 100 vs 0.
LEAD_TIME_FLOOR_DAYS = 1.0


# ---------------------------------------------------------------------------
# qualification questionnaire
# ---------------------------------------------------------------------------
def score_questionnaire(
	answers: Iterable[QuestionnaireAnswer],
	pass_threshold_pct: float = 70.0,
	risk_bands: Sequence[tuple[float, str]] | None = None,
) -> QualificationResult:
	"""Score a qualification questionnaire.

	Weighted percent is ``Σ(score x weight) / Σ(max_score x weight) x 100``.
	Rows whose ``weight`` or ``max_score`` is 0 or less contribute nothing to
	that ratio but are still evaluated for knockouts.

	Any failed knockout question forces ``passed`` False regardless of the
	score (the failing ``question_key`` values are listed in
	``knockout_failures``) and pins the risk tier to the last / highest-risk
	band - a supplier that fails a compliance gate is never "Low risk".
	"""
	bands = _normalize_risk_bands(risk_bands)

	total = 0.0
	maximum = 0.0
	knockout_failures: list[str] = []

	for answer in answers:
		weight = float(answer.weight or 0.0)
		max_score = float(answer.max_score or 0.0)
		score = float(answer.score or 0.0)

		if weight > EPSILON and max_score > EPSILON:
			score = min(max(score, 0.0), max_score)
			total += score * weight
			maximum += max_score * weight

		if answer.is_knockout and not answer.passed:
			knockout_failures.append(answer.question_key)

	percent = (100.0 * total / maximum) if maximum > EPSILON else 0.0
	percent = round(percent, 2)

	passed = percent >= float(pass_threshold_pct or 0.0) - EPSILON and not knockout_failures
	risk_tier = bands[-1][1] if knockout_failures else _risk_tier(percent, bands)

	return QualificationResult(
		total_score=round(total, 6),
		max_score=round(maximum, 6),
		percent=percent,
		passed=passed,
		knockout_failures=knockout_failures,
		risk_tier=risk_tier,
	)


def _normalize_risk_bands(risk_bands) -> list[tuple[float, str]]:
	"""Sort bands by minimum percent descending and guarantee a catch-all."""
	bands = [(float(min_pct), tier) for min_pct, tier in (risk_bands or DEFAULT_RISK_BANDS)]
	if not bands:
		bands = [(float(m), t) for m, t in DEFAULT_RISK_BANDS]
	bands.sort(key=lambda band: band[0], reverse=True)
	if bands[-1][0] > EPSILON:
		bands.append((0.0, RISK_HIGH))
	return bands


def _risk_tier(percent: float, bands: Sequence[tuple[float, str]]) -> str:
	"""First band (highest minimum first) whose minimum the percent reaches.

	Boundaries are inclusive: exactly 85% with the default bands is "Low".
	"""
	for min_pct, tier in bands:
		if percent >= min_pct - EPSILON:
			return tier
	return bands[-1][1]


# ---------------------------------------------------------------------------
# document requirements
# ---------------------------------------------------------------------------
def evaluate_documents(
	docs: Iterable[DocumentRequirement],
	as_of: datetime.date,
	expiring_within_days: int = 30,
) -> list[DocumentStatus]:
	"""Classify each required document as Valid / Expiring / Expired / Missing.

	* nothing attached -> ``Missing`` (regardless of expiry data)
	* attached, no expiry date -> ``Valid`` with ``days_to_expiry`` None
	  ("valid, no expiry" - e.g. a signed code of conduct)
	* expiry strictly before ``as_of`` -> ``Expired`` (negative days)
	* expiry within ``expiring_within_days`` (inclusive, so an expiry exactly
	  ``expiring_within_days`` away is already ``Expiring``; expiring *today*
	  is ``Expiring``, not yet ``Expired``)
	* otherwise ``Valid``
	"""
	as_of = _as_date(as_of)
	window = int(expiring_within_days or 0)
	statuses: list[DocumentStatus] = []

	for doc in docs:
		expiry = _as_date(doc.expiry_date) if doc.expiry_date else None

		if not doc.provided:
			statuses.append(
				DocumentStatus(
					doc_type=doc.doc_type,
					expiry_date=expiry,
					days_to_expiry=None,
					status=MISSING,
					is_mandatory=bool(doc.is_mandatory),
				)
			)
			continue

		if expiry is None:
			statuses.append(
				DocumentStatus(
					doc_type=doc.doc_type,
					expiry_date=None,
					days_to_expiry=None,
					status=VALID,
					is_mandatory=bool(doc.is_mandatory),
				)
			)
			continue

		days = (expiry - as_of).days
		if days < 0:
			status = EXPIRED
		elif days <= window:
			status = EXPIRING
		else:
			status = VALID

		statuses.append(
			DocumentStatus(
				doc_type=doc.doc_type,
				expiry_date=expiry,
				days_to_expiry=days,
				status=status,
				is_mandatory=bool(doc.is_mandatory),
			)
		)

	return statuses


def documents_ok(statuses: Iterable[DocumentStatus], mandatory_only: bool = False) -> bool:
	"""True when no document is Expired or Missing.

	``mandatory_only`` limits the check to documents flagged mandatory, which
	is what the qualification-status report shows as "Mandatory Docs OK".
	"""
	for status in statuses:
		if mandatory_only and not status.is_mandatory:
			continue
		if status.status in BLOCKING_DOCUMENT_STATUSES:
			return False
	return True


def count_expiring(statuses: Iterable[DocumentStatus]) -> int:
	"""Number of documents in the Expiring window."""
	return sum(1 for status in statuses if status.status == EXPIRING)


def _as_date(value) -> datetime.date:
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value)[:10])


# ---------------------------------------------------------------------------
# bid scoring
# ---------------------------------------------------------------------------
def score_bids(
	bid_lines: Iterable[BidLine],
	weights: Mapping[str, float] | None = None,
	scorecard_by_supplier: Mapping[str, float] | None = None,
	qualification_by_supplier: Mapping[str, float] | None = None,
) -> list[AwardScore]:
	"""Rank the suppliers bidding on an event.

	Price and lead time are normalized **per item** (suppliers rarely bid the
	same basket, so a raw total would just reward whoever bid fewest lines)
	and then aggregated to a supplier score as a qty-weighted mean over the
	items that supplier bid:

	* ``price_score = best_price / this_price x 100`` per item. A line priced
	  0 or less is a data error, not a winning bid: it is excluded from the
	  best-price search and scores 0.
	* ``lead_time_score = (best_lead + 1) / (this_lead + 1) x 100`` per item -
	  see :data:`LEAD_TIME_FLOOR_DAYS`. Negative lead times are floored at 0,
	  and when every lead time is 0 everyone scores 100.

	``scorecard_by_supplier`` / ``qualification_by_supplier`` are already on a
	0-100 scale (Supplier Scorecard ``supplier_score`` and the qualification
	percent). A supplier missing from either map scores 0 for that component
	but is still ranked.

	The weighted total is ``Σ(weight x component) / Σ(weight)``; components
	with a 0 (or missing) weight drop out entirely. When every weight is 0 the
	totals are 0 - the Sourcing Event controller rejects that setup up front.

	Ranks are 1-based over ``weighted_total`` descending, ties broken by
	supplier name ascending, so repeated runs return an identical order.
	"""
	lines = [line for line in bid_lines if line.supplier and line.item_code]
	weights = _normalize_weights(weights)
	scorecards = dict(scorecard_by_supplier or {})
	qualifications = dict(qualification_by_supplier or {})

	suppliers = sorted({line.supplier for line in lines})
	by_item: dict[str, list[BidLine]] = {}
	for line in lines:
		by_item.setdefault(line.item_code, []).append(line)

	price_points: dict[str, list[tuple[float, float]]] = {s: [] for s in suppliers}
	lead_points: dict[str, list[tuple[float, float]]] = {s: [] for s in suppliers}

	for item_lines in by_item.values():
		priced = [line for line in item_lines if float(line.unit_price or 0.0) > EPSILON]
		best_price = min((float(line.unit_price) for line in priced), default=0.0)
		best_lead = min((max(float(line.lead_time_days or 0.0), 0.0) for line in item_lines), default=0.0)

		for line in item_lines:
			qty = max(float(line.qty or 0.0), 0.0)
			price = float(line.unit_price or 0.0)
			if price > EPSILON and best_price > EPSILON:
				price_score = 100.0 * best_price / price
			else:
				price_score = 0.0

			lead = max(float(line.lead_time_days or 0.0), 0.0)
			lead_score = 100.0 * (best_lead + LEAD_TIME_FLOOR_DAYS) / (lead + LEAD_TIME_FLOOR_DAYS)

			price_points[line.supplier].append((price_score, qty))
			lead_points[line.supplier].append((lead_score, qty))

	scores: list[AwardScore] = []
	for supplier in suppliers:
		components = {
			W_PRICE: _weighted_mean(price_points[supplier]),
			W_LEAD_TIME: _weighted_mean(lead_points[supplier]),
			W_SCORECARD: _clamp_percent(scorecards.get(supplier)),
			W_QUALIFICATION: _clamp_percent(qualifications.get(supplier)),
		}

		weight_sum = sum(weights.values())
		if weight_sum > EPSILON:
			total = sum(weights[key] * components[key] for key in components) / weight_sum
		else:
			total = 0.0

		scores.append(
			AwardScore(
				supplier=supplier,
				price_score=round(components[W_PRICE], 2),
				lead_time_score=round(components[W_LEAD_TIME], 2),
				scorecard_score=round(components[W_SCORECARD], 2),
				qualification_score=round(components[W_QUALIFICATION], 2),
				weighted_total=round(total, 2),
			)
		)

	scores.sort(key=lambda score: (-score.weighted_total, score.supplier))
	for index, score in enumerate(scores, start=1):
		score.rank = index

	return scores


def _normalize_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
	source = weights if weights is not None else DEFAULT_WEIGHTS
	return {
		W_PRICE: max(float(source.get(W_PRICE) or 0.0), 0.0),
		W_LEAD_TIME: max(float(source.get(W_LEAD_TIME) or 0.0), 0.0),
		W_SCORECARD: max(float(source.get(W_SCORECARD) or 0.0), 0.0),
		W_QUALIFICATION: max(float(source.get(W_QUALIFICATION) or 0.0), 0.0),
	}


def _weighted_mean(points: Sequence[tuple[float, float]]) -> float:
	"""Qty-weighted mean of per-item scores; unweighted when all qty are 0."""
	if not points:
		return 0.0
	weight_sum = sum(qty for _score, qty in points)
	if weight_sum > EPSILON:
		return sum(score * qty for score, qty in points) / weight_sum
	return sum(score for score, _qty in points) / len(points)


def _clamp_percent(value) -> float:
	if value is None:
		return 0.0
	try:
		return min(max(float(value), 0.0), 100.0)
	except (TypeError, ValueError):
		return 0.0


# ---------------------------------------------------------------------------
# award scenarios
# ---------------------------------------------------------------------------
def build_award_scenarios(
	bid_lines: Iterable[BidLine],
	award_scores: Iterable[AwardScore] | None = None,
	item_codes: Iterable[str] | None = None,
) -> list[AwardScenario]:
	"""Build the three side-by-side award proposals.

	``item_codes`` is the event's demanded basket - pass it so items nobody
	bid show up as ``coverage_gaps``. When omitted, the basket is inferred
	from the bid lines (and the first two scenarios then have no gaps).

	* **Lowest Price** - per item, the cheapest valid line (tie: supplier name).
	* **Best Weighted** - per item, the bidder with the highest
	  ``weighted_total`` from :func:`score_bids` (tie: cheaper line, then
	  supplier name).
	* **Single Supplier** - the one supplier covering the most items; ties go
	  to the cheapest basket over the items covered, then supplier name. Items
	  that supplier did not bid are its coverage gaps.

	Only lines with a unit price above 0 can be awarded; a 0/negative price is
	treated as "did not bid" (same guard as :func:`score_bids`).
	"""
	lines = [
		line
		for line in bid_lines
		if line.supplier and line.item_code and float(line.unit_price or 0.0) > EPSILON
	]
	totals = {score.supplier: score.weighted_total for score in (award_scores or [])}

	basket = list(dict.fromkeys(item_codes)) if item_codes is not None else []
	if not basket:
		basket = sorted({line.item_code for line in lines})

	by_item: dict[str, list[BidLine]] = {}
	for line in lines:
		by_item.setdefault(line.item_code, []).append(line)

	lowest = _scenario(
		SCENARIO_LOWEST_PRICE,
		basket,
		by_item,
		key=lambda line: (float(line.unit_price), line.supplier),
	)
	best_weighted = _scenario(
		SCENARIO_BEST_WEIGHTED,
		basket,
		by_item,
		key=lambda line: (-totals.get(line.supplier, 0.0), float(line.unit_price), line.supplier),
	)
	single = _single_supplier_scenario(basket, by_item)

	return [lowest, best_weighted, single]


def _scenario(name, basket, by_item, key) -> AwardScenario:
	awards: list[tuple[str, str, float, float]] = []
	gaps: list[str] = []

	for item_code in basket:
		candidates = by_item.get(item_code) or []
		if not candidates:
			gaps.append(item_code)
			continue
		winner = min(candidates, key=key)
		awards.append((item_code, winner.supplier, float(winner.qty or 0.0), float(winner.unit_price)))

	return AwardScenario(
		name=name,
		awards=awards,
		total_cost=round(sum(qty * price for _i, _s, qty, price in awards), 6),
		coverage_gaps=gaps,
	)


def _single_supplier_scenario(basket, by_item) -> AwardScenario:
	basket_set = set(basket)
	candidates: dict[str, dict[str, BidLine]] = {}

	for item_code, item_lines in by_item.items():
		if item_code not in basket_set:
			continue
		for line in item_lines:
			# cheapest line per supplier+item, in case a supplier bid a line twice
			current = candidates.setdefault(line.supplier, {}).get(item_code)
			if current is None or float(line.unit_price) < float(current.unit_price):
				candidates[line.supplier][item_code] = line

	if not candidates:
		return AwardScenario(
			name=SCENARIO_SINGLE_SUPPLIER, awards=[], total_cost=0.0, coverage_gaps=list(basket)
		)

	def basket_cost(supplier: str) -> float:
		return sum(float(line.qty or 0.0) * float(line.unit_price) for line in candidates[supplier].values())

	winner = min(
		candidates, key=lambda supplier: (-len(candidates[supplier]), basket_cost(supplier), supplier)
	)
	won = candidates[winner]

	awards = [
		(item_code, winner, float(won[item_code].qty or 0.0), float(won[item_code].unit_price))
		for item_code in basket
		if item_code in won
	]

	return AwardScenario(
		name=SCENARIO_SINGLE_SUPPLIER,
		awards=awards,
		total_cost=round(sum(qty * price for _i, _s, qty, price in awards), 6),
		coverage_gaps=[item_code for item_code in basket if item_code not in won],
	)
