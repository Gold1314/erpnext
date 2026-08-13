# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure domain model for the semantic metric layer.

No frappe imports here - these dataclasses are plain Python so the engine and
the KPI registry can be unit-tested without a site (same doctrine as
``erpnext/accounts/forecasting/models.py``).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
CURRENCY = "Currency"
PERCENT = "Percent"
DAYS = "Days"
RATIO = "Ratio"
COUNT = "Count"
UNITS = (CURRENCY, PERCENT, DAYS, RATIO, COUNT)

#: decimal places applied by :func:`erpnext.analytics.engine.round_for_unit`
UNIT_PRECISION = {
	CURRENCY: 2,
	PERCENT: 2,
	DAYS: 1,
	RATIO: 3,
	COUNT: 0,
}

# ---------------------------------------------------------------------------
# Grain
# ---------------------------------------------------------------------------
#: value is a point-in-time balance; only the period *end* matters
GRAIN_COMPANY = "Company"
#: value is a flow measured over the period; both dates matter
GRAIN_COMPANY_PERIOD = "Company+Period"
GRAINS = (GRAIN_COMPANY, GRAIN_COMPANY_PERIOD)

# ---------------------------------------------------------------------------
# Direction - how to read "good"
# ---------------------------------------------------------------------------
HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
NEUTRAL = "neutral"
DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER, NEUTRAL)

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
GREEN = "Green"
AMBER = "Amber"
RED = "Red"
UNKNOWN = "Unknown"
STATUSES = (GREEN, AMBER, RED, UNKNOWN)

# ---------------------------------------------------------------------------
# Categories (report grouping order)
# ---------------------------------------------------------------------------
LIQUIDITY = "Liquidity"
RECEIVABLES = "Receivables"
PROFITABILITY = "Profitability"
GROWTH = "Growth"
OPERATIONS = "Operations"
GOVERNANCE = "Governance"
CATEGORIES = (LIQUIDITY, RECEIVABLES, PROFITABILITY, GROWTH, OPERATIONS, GOVERNANCE)


@dataclass(frozen=True)
class Threshold:
	"""RAG bands for one metric.

	The three numbers are *boundaries*, and :attr:`MetricSpec.direction`
	decides which side of a boundary is good. Semantics (evaluated in this
	order by :func:`erpnext.analytics.engine.evaluate_threshold`):

	``higher_is_better``
	    - ``value < red_max``      -> **Red**   (hard breach; ``red_max`` is a floor)
	    - ``value >= green_min``   -> **Green**
	    - ``value >= amber_min``   -> **Amber**
	    - otherwise                -> **Red**

	``lower_is_better``
	    - ``value > red_max``      -> **Red**   (hard breach; ``red_max`` is a ceiling)
	    - ``value <= green_min``   -> **Green** (``green_min`` is the "at best" ceiling)
	    - ``value <= amber_min``   -> **Amber**
	    - otherwise                -> **Red**

	``neutral``
	    always **Unknown** - a neutral metric has no better/worse side, so a
	    RAG judgement would be meaningless.

	Any bound may be ``None`` (not configured) and is then skipped. If no
	bound at all is configured the status is **Unknown**, never Red - an
	unconfigured metric must not look like a failing one. If only ``red_max``
	is configured, not breaching it is **Green**.
	"""

	key: str
	green_min: float | None = None
	amber_min: float | None = None
	red_max: float | None = None

	@property
	def is_configured(self) -> bool:
		return any(bound is not None for bound in (self.green_min, self.amber_min, self.red_max))


@dataclass(frozen=True)
class MetricSpec:
	"""One governed metric definition - the unit of the catalog.

	``inputs`` are the *named numeric inputs* the arithmetic needs;
	``loaders.py`` is responsible for supplying exactly these names.
	``fn`` is the pure arithmetic: it receives the resolved input dict and a
	calculator that owns the divide-by-zero guards, and returns a number or
	``None``. It must never raise (the engine catches anyway).
	"""

	key: str
	label: str
	unit: str
	grain: str
	direction: str
	category: str
	description: str
	formula_text: str
	inputs: list[str]
	fn: Callable[[dict, object], float | None] | None = None
	#: shipped starting point for thresholds; ``None`` where the right band is
	#: a judgement call and must be set per company.
	default_threshold: Threshold | None = None

	def validate(self) -> list[str]:
		"""Return a list of problems with this spec (empty == valid)."""
		problems = []
		if not self.key or self.key != self.key.strip().lower().replace(" ", "_"):
			problems.append(f"{self.key!r}: key must be a lower_snake_case identifier")
		if not self.label:
			problems.append(f"{self.key}: label is empty")
		if self.unit not in UNITS:
			problems.append(f"{self.key}: unit {self.unit!r} not in {UNITS}")
		if self.grain not in GRAINS:
			problems.append(f"{self.key}: grain {self.grain!r} not in {GRAINS}")
		if self.direction not in DIRECTIONS:
			problems.append(f"{self.key}: direction {self.direction!r} not in {DIRECTIONS}")
		if self.category not in CATEGORIES:
			problems.append(f"{self.key}: category {self.category!r} not in {CATEGORIES}")
		if not self.formula_text:
			problems.append(f"{self.key}: formula_text is empty")
		if not self.description:
			problems.append(f"{self.key}: description is empty")
		if not self.inputs:
			problems.append(f"{self.key}: no inputs declared")
		if len(set(self.inputs)) != len(self.inputs):
			problems.append(f"{self.key}: duplicate input names")
		if self.fn is None:
			problems.append(f"{self.key}: no arithmetic (fn) attached")
		if self.default_threshold and self.default_threshold.key != self.key:
			problems.append(f"{self.key}: default_threshold.key mismatch")
		return problems

	def as_dict(self) -> dict:
		"""Serializable view for UIs, agents and fixtures (drops ``fn``)."""
		threshold = self.default_threshold
		return {
			"key": self.key,
			"label": self.label,
			"unit": self.unit,
			"grain": self.grain,
			"direction": self.direction,
			"category": self.category,
			"description": self.description,
			"formula_text": self.formula_text,
			"inputs": list(self.inputs),
			"default_green_min": threshold.green_min if threshold else None,
			"default_amber_min": threshold.amber_min if threshold else None,
			"default_red_max": threshold.red_max if threshold else None,
		}


@dataclass
class MetricValue:
	"""One computed number for one company and one period."""

	key: str
	company: str | None = None
	period_start: datetime.date | None = None
	period_end: datetime.date | None = None
	value: float | None = None
	inputs_used: dict = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)

	@property
	def is_available(self) -> bool:
		return self.value is not None

	def as_dict(self) -> dict:
		return {
			"key": self.key,
			"company": self.company,
			"period_start": self.period_start.isoformat() if self.period_start else None,
			"period_end": self.period_end.isoformat() if self.period_end else None,
			"value": self.value,
			"inputs_used": dict(self.inputs_used),
			"warnings": list(self.warnings),
		}
