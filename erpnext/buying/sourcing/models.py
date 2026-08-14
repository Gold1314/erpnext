# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for supplier qualification + sourcing events.

No frappe imports here - these dataclasses are plain Python so the engine can
be unit-tested without a site (same shape as ``erpnext/stock/wms/models.py``
and ``erpnext/accounts/forecasting``).

Scores are always expressed on a 0-100 scale once normalized, so a
questionnaire percent, a Supplier Scorecard ``supplier_score`` and a bid
component score are directly comparable in the weighted award math.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# --- document statuses -------------------------------------------------
VALID = "Valid"
EXPIRING = "Expiring"
EXPIRED = "Expired"
MISSING = "Missing"

DOCUMENT_STATUSES = (VALID, EXPIRING, EXPIRED, MISSING)

#: statuses that make a document set unacceptable (see ``engine.documents_ok``)
BLOCKING_DOCUMENT_STATUSES = (EXPIRED, MISSING)

# --- risk tiers --------------------------------------------------------
RISK_LOW = "Low"
RISK_MEDIUM = "Medium"
RISK_HIGH = "High"

RISK_TIERS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

#: ``(min_percent, tier)`` sorted by ``min_percent`` descending. The last band
#: is the catch-all and must have ``min_percent`` 0.
DEFAULT_RISK_BANDS = ((85.0, RISK_LOW), (70.0, RISK_MEDIUM), (0.0, RISK_HIGH))

# --- award-scenario names ----------------------------------------------
SCENARIO_LOWEST_PRICE = "Lowest Price"
SCENARIO_BEST_WEIGHTED = "Best Weighted"
SCENARIO_SINGLE_SUPPLIER = "Single Supplier"

SCENARIO_NAMES = (SCENARIO_LOWEST_PRICE, SCENARIO_BEST_WEIGHTED, SCENARIO_SINGLE_SUPPLIER)

# --- bid-scoring component keys (keys of the ``weights`` mapping) -------
W_PRICE = "price"
W_LEAD_TIME = "lead_time"
W_SCORECARD = "scorecard"
W_QUALIFICATION = "qualification"

WEIGHT_KEYS = (W_PRICE, W_LEAD_TIME, W_SCORECARD, W_QUALIFICATION)

#: default award weights (mirror the Sourcing Event field defaults)
DEFAULT_WEIGHTS = {W_PRICE: 50.0, W_LEAD_TIME: 20.0, W_SCORECARD: 20.0, W_QUALIFICATION: 10.0}


@dataclass
class QuestionnaireAnswer:
	"""One answered qualification question.

	``score`` is clamped into ``[0, max_score]`` by the engine. ``weight`` and
	``max_score`` of 0 (or less) drop the row out of the weighted average but
	the row still counts for knockout evaluation - a knockout question that is
	not scored at all can still fail the supplier.
	"""

	question_key: str
	weight: float = 1.0
	score: float = 0.0
	max_score: float = 5.0
	is_knockout: bool = False
	passed: bool = True


@dataclass
class QualificationResult:
	"""Outcome of :func:`engine.score_questionnaire`."""

	total_score: float
	max_score: float
	percent: float
	passed: bool
	knockout_failures: list[str] = field(default_factory=list)
	risk_tier: str = RISK_HIGH


@dataclass
class DocumentRequirement:
	"""A required supplier document as the engine sees it.

	``provided`` is True once a file is attached. ``expiry_date`` may be None
	either because the document type never expires (``requires_expiry`` off)
	or because the buyer has not captured it yet.
	"""

	doc_type: str
	is_mandatory: bool = True
	requires_expiry: bool = False
	provided: bool = False
	expiry_date: datetime.date | None = None


@dataclass
class DocumentStatus:
	"""Classification of one required document at a point in time."""

	doc_type: str
	expiry_date: datetime.date | None = None
	days_to_expiry: int | None = None
	status: str = MISSING
	is_mandatory: bool = True


@dataclass
class BidLine:
	"""One item line of one supplier's bid on a sourcing event."""

	supplier: str
	item_code: str
	qty: float = 0.0
	unit_price: float = 0.0
	lead_time_days: float = 0.0


@dataclass
class AwardScore:
	"""Per-supplier weighted standing over a sourcing event's bids."""

	supplier: str
	price_score: float = 0.0
	lead_time_score: float = 0.0
	scorecard_score: float = 0.0
	qualification_score: float = 0.0
	weighted_total: float = 0.0
	rank: int = 0


@dataclass
class AwardScenario:
	"""One award proposal over a sourcing event's bids.

	``awards`` are ``(item_code, supplier, qty, price)`` tuples - ``price`` is
	the unit price, so ``total_cost`` is ``sum(qty * price)``.
	``coverage_gaps`` lists item codes the scenario cannot award (nobody bid,
	or - for Single Supplier - the chosen supplier did not bid them).
	"""

	name: str
	awards: list[tuple[str, str, float, float]] = field(default_factory=list)
	total_cost: float = 0.0
	coverage_gaps: list[str] = field(default_factory=list)
