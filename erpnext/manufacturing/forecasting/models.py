# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure data model for the statistical demand-forecasting engine.

No frappe, no numpy, no ambient time — plain dataclasses and date arithmetic
so everything here is unit-testable without a site (see the scheduling
engine's pure-core + adapter doctrine in ``erpnext/manufacturing/scheduling``).

Conventions used across the package:

- A demand series has a fixed periodicity (``Weekly`` or ``Monthly``) and is
  keyed by **period start dates** (Monday for weekly buckets, first of the
  month for monthly buckets).
- Forecast error is ``forecast - actual`` (positive bias = over-forecasting).
- ``mape`` is a percentage (0-100); ``bias`` is a fraction
  (mean error / mean actual).
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, field

WEEKLY = "Weekly"
MONTHLY = "Monthly"
PERIODICITIES = (WEEKLY, MONTHLY)

DEFAULT_SEASON_LENGTH = {WEEKLY: 52, MONTHLY: 12}


# ---------------------------------------------------------------------------
# Period arithmetic (pure — datetime only)
# ---------------------------------------------------------------------------


def _as_date(value) -> datetime.date:
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value))


def period_start(date, periodicity: str) -> datetime.date:
	"""Normalize any date to the start of its demand bucket.

	Weekly buckets start on Monday (ISO); monthly buckets on the 1st.
	"""
	date = _as_date(date)
	if periodicity == WEEKLY:
		return date - datetime.timedelta(days=date.weekday())
	if periodicity == MONTHLY:
		return date.replace(day=1)
	raise ValueError(f"Unsupported periodicity: {periodicity!r}")


def add_periods(date, n: int, periodicity: str) -> datetime.date:
	"""Shift a period-start date by ``n`` whole periods."""
	date = _as_date(date)
	if periodicity == WEEKLY:
		return date + datetime.timedelta(weeks=n)
	if periodicity == MONTHLY:
		month_index = date.year * 12 + (date.month - 1) + n
		year, month = divmod(month_index, 12)
		month += 1
		day = min(date.day, calendar.monthrange(year, month)[1])
		return datetime.date(year, month, day)
	raise ValueError(f"Unsupported periodicity: {periodicity!r}")


def next_period_start(date, periodicity: str) -> datetime.date:
	"""Start date of the period immediately after the one containing ``date``."""
	return add_periods(period_start(date, periodicity), 1, periodicity)


def period_range(from_date, to_date, periodicity: str) -> list[datetime.date]:
	"""All period-start dates covering ``[from_date, to_date]``, inclusive."""
	start = period_start(from_date, periodicity)
	end = period_start(to_date, periodicity)

	periods = []
	current = start
	while current <= end:
		periods.append(current)
		current = add_periods(current, 1, periodicity)

	return periods


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TimeSeries:
	"""A demand history with fixed periodicity.

	``points`` is an ordered list of ``(period_start_date, qty)`` tuples with
	**no gaps** — loaders zero-fill missing periods so the statistical models
	see true zero-demand periods (crucial for intermittency detection and for
	seasonal indexing).
	"""

	periodicity: str
	points: list[tuple[datetime.date, float]] = field(default_factory=list)

	def values(self) -> list[float]:
		return [qty for _, qty in self.points]

	def dates(self) -> list[datetime.date]:
		return [date for date, _ in self.points]

	def value_at(self, date) -> float:
		"""Demand in the bucket containing ``date`` (0.0 if outside range)."""
		key = period_start(date, self.periodicity)
		for point_date, qty in self.points:
			if point_date == key:
				return qty
		return 0.0

	def zero_ratio(self) -> float:
		if not self.points:
			return 0.0
		zeros = sum(1 for _, qty in self.points if not qty)
		return zeros / len(self.points)

	def __len__(self) -> int:
		return len(self.points)


@dataclass
class ForecastPoint:
	date: datetime.date
	qty: float
	lower: float | None = None
	upper: float | None = None


@dataclass
class ModelFit:
	"""Result of fitting one model on a full history.

	- ``fitted``: one-step-ahead in-sample predictions aligned with the
	  history (``None`` where the model was not yet primed).
	- ``forecast``: out-of-sample point forecasts, clamped at zero.
	- ``metrics``: mape / bias / mad / rmse / n_folds (from backtesting when
	  available, in-sample otherwise — ``metrics["source"]`` says which).
	"""

	model_name: str
	params: dict
	fitted: list
	forecast: list
	metrics: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
	model_name: str
	mape: float | None
	bias: float | None
	mad: float | None
	rmse: float | None
	n_folds: int


def to_forecast_points(start_date, values, periodicity: str) -> list[ForecastPoint]:
	"""Attach period dates to raw forecast values.

	``start_date`` is the start of the **first forecast period** (i.e. the
	period after the last history period).
	"""
	start = period_start(start_date, periodicity)
	return [
		ForecastPoint(date=add_periods(start, index, periodicity), qty=qty)
		for index, qty in enumerate(values)
	]
