# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure evaluation engine for the semantic metric layer.

Takes a :class:`MetricSpec` plus a dict of named numeric inputs and returns a
:class:`MetricValue`. All arithmetic hazards (division by zero, missing
inputs, non-numeric inputs) degrade to ``value = None`` plus a warning - the
engine never raises, because a KPI board must render even when one feed is
cold.

No frappe imports and no ambient "now": callers pass company/period.
"""

from __future__ import annotations

import datetime

from erpnext.analytics.models import (
	AMBER,
	COUNT,
	GREEN,
	HIGHER_IS_BETTER,
	LOWER_IS_BETTER,
	NEUTRAL,
	RED,
	UNIT_PRECISION,
	UNKNOWN,
	MetricSpec,
	MetricValue,
	Threshold,
)


class Calc:
	"""Guarded arithmetic handed to every metric's ``fn``.

	Every division in the registry goes through :meth:`div` so a zero or
	missing denominator produces ``None`` + a warning instead of an exception.
	"""

	def __init__(self, key: str = ""):
		self.key = key
		self.warnings: list[str] = []
		#: declared inputs that could not be resolved to a number
		self.missing: list[str] = []

	def warn(self, message: str) -> None:
		if message not in self.warnings:
			self.warnings.append(message)

	def div(self, numerator, denominator, denominator_label: str = "denominator"):
		"""Return ``numerator / denominator`` or ``None`` (+warning) if undefined."""
		num = _num(numerator)
		den = _num(denominator)
		if num is None:
			self.warn(f"{self.key or 'metric'}: numerator is not a number")
			return None
		if den is None:
			self.warn(f"{self.key or 'metric'}: {denominator_label} is not a number")
			return None
		if den == 0:
			self.warn(f"{self.key or 'metric'}: {denominator_label} is zero - value undefined")
			return None
		return num / den

	def pct(self, numerator, denominator, denominator_label: str = "denominator"):
		"""Percentage form of :meth:`div` (``100 * n / d``)."""
		result = self.div(numerator, denominator, denominator_label)
		return None if result is None else 100.0 * result

	def value(self, raw):
		"""Pass a plain number through, warning when it is not numeric."""
		num = _num(raw)
		if num is None:
			self.warn(f"{self.key or 'metric'}: value is not a number")
		return num


def _num(value):
	"""Coerce to float, or ``None`` when that is not possible."""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, int | float):
		result = float(value)
		return None if result != result else result  # NaN check
	try:
		return float(str(value).strip())
	except (TypeError, ValueError):
		return None


def round_for_unit(value, unit: str):
	"""Round ``value`` to the precision this unit is reported at."""
	if value is None:
		return None
	precision = UNIT_PRECISION.get(unit, 2)
	rounded = round(float(value), precision)
	return int(rounded) if precision == 0 else rounded


def resolve_bounds(green_min, amber_min, red_max, unit: str) -> tuple:
	"""Resolve stored threshold bounds to (green, amber, red), ``None`` = unset.

	The single definition of "is this bound set?", shared by the runtime
	grading path and Metric Definition's validation so the two can never
	disagree about a saved row. Frappe ``Float`` columns are ``NOT NULL
	DEFAULT 0``, so a stored ``0`` cannot be distinguished from blank:

	1. a row whose three bounds are all ``0`` configures nothing;
	2. otherwise ``0`` is a real bound only for ``Count`` metrics, where zero
	   open findings is the natural target; for money, ratio, percent and days
	   metrics it means unset.
	"""
	bounds = (green_min, amber_min, red_max)
	if not any(bounds):
		return (None, None, None)

	zero_is_a_bound = unit == COUNT

	def bound(value):
		if value is None:
			return None
		if value == 0 and not zero_is_a_bound:
			return None
		return value

	return tuple(bound(value) for value in bounds)


def resolve_inputs(spec: MetricSpec, inputs: dict | None, calc: Calc) -> dict:
	"""Pull the spec's declared inputs out of ``inputs``.

	A missing or non-numeric input is recorded in :attr:`Calc.missing` and
	placeholdered with ``0.0`` for the audit trail; :func:`compute_metric` then
	reports the metric as unavailable rather than computing on the placeholder.
	Reporting a falsely-good ``0`` (a DSO of zero days because the receivable
	feed was down) would be worse than reporting nothing.

	Loaders that legitimately have nothing to count - a module the customer has
	not used yet - pass a real ``0`` with their own warning, which computes
	normally. Denominator hazards are handled separately by :meth:`Calc.div`.
	"""
	inputs = inputs or {}
	resolved = {}
	for name in spec.inputs:
		if name not in inputs or inputs.get(name) is None:
			calc.warn(f"{spec.key}: input '{name}' unavailable - metric not computed")
			calc.missing.append(name)
			resolved[name] = 0.0
			continue
		number = _num(inputs.get(name))
		if number is None:
			calc.warn(f"{spec.key}: input '{name}' is not numeric - metric not computed")
			calc.missing.append(name)
			number = 0.0
		resolved[name] = number
	return resolved


def compute_metric(
	spec: MetricSpec,
	inputs: dict | None = None,
	company: str | None = None,
	period_start: datetime.date | None = None,
	period_end: datetime.date | None = None,
) -> MetricValue:
	"""Evaluate one metric. Never raises.

	Returns a :class:`MetricValue` whose ``value`` is rounded per
	:data:`~erpnext.analytics.models.UNIT_PRECISION`, or ``None`` when the
	arithmetic is undefined (with the reason in ``warnings``).
	"""
	calc = Calc(spec.key)
	resolved = resolve_inputs(spec, inputs, calc)

	raw = None
	if calc.missing:
		pass  # unavailable inputs already warned; value stays None
	elif spec.fn is None:
		calc.warn(f"{spec.key}: no arithmetic defined")
	else:
		try:
			raw = spec.fn(resolved, calc)
		except ZeroDivisionError:
			calc.warn(f"{spec.key}: division by zero - value undefined")
			raw = None
		except Exception as e:  # never let one metric break the board
			calc.warn(f"{spec.key}: computation failed ({type(e).__name__}: {e})")
			raw = None

	if raw is not None and _num(raw) is None:
		calc.warn(f"{spec.key}: result is not a number")
		raw = None

	return MetricValue(
		key=spec.key,
		company=company,
		period_start=period_start,
		period_end=period_end,
		value=round_for_unit(_num(raw), spec.unit) if raw is not None else None,
		inputs_used=resolved,
		warnings=list(calc.warnings),
	)


def compute_many(
	specs,
	inputs_by_key: dict | None = None,
	company: str | None = None,
	period_start: datetime.date | None = None,
	period_end: datetime.date | None = None,
	shared_inputs: dict | None = None,
) -> list[MetricValue]:
	"""Compute a list of specs.

	``inputs_by_key`` maps ``metric_key -> input dict``; ``shared_inputs`` is
	a single flat pool used for any spec without a dedicated entry (that is
	how ``loaders.collect_inputs`` feeds the whole pack in one pass).
	"""
	inputs_by_key = inputs_by_key or {}
	results = []
	for spec in specs:
		metric_inputs = inputs_by_key.get(spec.key, shared_inputs or {})
		results.append(compute_metric(spec, metric_inputs, company, period_start, period_end))
	return results


def evaluate_threshold(spec: MetricSpec, value, threshold: Threshold | None) -> str:
	"""Return ``Green`` / ``Amber`` / ``Red`` / ``Unknown``.

	Band semantics are documented on :class:`~erpnext.analytics.models.Threshold`:
	the bounds are boundaries and ``spec.direction`` decides which side is
	good. ``neutral`` metrics are always ``Unknown``.
	"""
	number = _num(value)
	if number is None:
		return UNKNOWN
	if threshold is None or not threshold.is_configured:
		return UNKNOWN
	if spec.direction == NEUTRAL:
		return UNKNOWN

	green_min, amber_min, red_max = threshold.green_min, threshold.amber_min, threshold.red_max

	if spec.direction == HIGHER_IS_BETTER:
		if red_max is not None and number < red_max:
			return RED
		if green_min is not None and number >= green_min:
			return GREEN
		if amber_min is not None and number >= amber_min:
			return AMBER
	elif spec.direction == LOWER_IS_BETTER:
		if red_max is not None and number > red_max:
			return RED
		if green_min is not None and number <= green_min:
			return GREEN
		if amber_min is not None and number <= amber_min:
			return AMBER
	else:  # pragma: no cover - guarded by DIRECTIONS validation
		return UNKNOWN

	if green_min is None and amber_min is None:
		# only a hard bound was configured and it was not breached
		return GREEN
	return RED


def trend(values: list[MetricValue], spec: MetricSpec | None = None) -> dict:
	"""Compare the newest value against the previous one.

	``values`` is ordered oldest -> newest. Returns::

	    {"current", "previous", "change", "pct_change", "direction_is_good", "sparkline"}

	Guards: a ``None`` current or previous value yields ``change = None``; a
	zero baseline yields ``pct_change = None`` (an infinite growth rate is not
	a number a CFO can use). ``direction_is_good`` is ``False`` when the
	comparison cannot be made or the metric is neutral.
	"""
	series = [v for v in (values or [])]
	sparkline = [v.value for v in series]
	current = series[-1].value if series else None
	previous = series[-2].value if len(series) >= 2 else None

	change = None
	pct_change = None
	if current is not None and previous is not None:
		change = round(current - previous, 6)
		if previous != 0:
			pct_change = round(100.0 * (current - previous) / abs(previous), 2)

	direction_is_good = False
	if change is not None and spec is not None:
		if spec.direction == HIGHER_IS_BETTER:
			direction_is_good = change > 0
		elif spec.direction == LOWER_IS_BETTER:
			direction_is_good = change < 0

	return {
		"current": current,
		"previous": previous,
		"change": change,
		"pct_change": pct_change,
		"direction_is_good": direction_is_good,
		"sparkline": sparkline,
	}
