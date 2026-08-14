# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure statistical forecasting models — no frappe, no numpy.

Every model is a class with ``fit(history: list[float])`` and
``forecast(horizon: int) -> list[float]``. Smoothing parameters left as
``None`` are chosen by a coarse grid search (values 0.1..0.9, step 0.2)
minimizing in-sample one-step-ahead MSE — fast, deterministic and
dependency-free. Point forecasts are clamped at zero (demand cannot be
negative).

Models
------
- ``NaiveForecast``            last observed value
- ``SeasonalNaive``            value from one season ago
- ``SingleExponentialSmoothing`` (alpha)
- ``HoltLinear``               (alpha, beta, optional damping phi)
- ``HoltWintersAdditive``      (alpha, beta, gamma, season_length)
- ``HoltWintersMultiplicative`` — falls back to additive when the history
  contains zeros/negatives (multiplicative decomposition is undefined there)
- ``CrostonForecast`` / ``CrostonSBA`` for intermittent demand
"""

from __future__ import annotations

from erpnext.manufacturing.forecasting.models import ModelFit

PARAM_GRID = (0.1, 0.3, 0.5, 0.7, 0.9)

NAIVE = "Naive"
SEASONAL_NAIVE = "Seasonal Naive"
EXPONENTIAL_SMOOTHING = "Exponential Smoothing"
HOLT_LINEAR = "Holt Linear"
HOLT_WINTERS_ADDITIVE = "Holt-Winters Additive"
HOLT_WINTERS_MULTIPLICATIVE = "Holt-Winters Multiplicative"
CROSTON = "Croston"
CROSTON_SBA = "Croston SBA"


def _mse(fitted: list, history: list[float]) -> float:
	"""In-sample one-step-ahead MSE, skipping unprimed (None) positions."""
	total, count = 0.0, 0
	for predicted, actual in zip(fitted, history, strict=True):
		if predicted is None:
			continue
		total += (predicted - actual) ** 2
		count += 1
	return total / count if count else float("inf")


def _clamp(values: list[float]) -> list[float]:
	return [value if value > 0.0 else 0.0 for value in values]


class BaseForecaster:
	name = "Base"
	min_history = 1

	def __init__(self):
		self.params: dict = {}
		self.fitted: list = []
		self._history: list[float] | None = None

	def fit(self, history: list[float]):
		history = [float(value) for value in history]
		if len(history) < self.min_history:
			raise ValueError(
				f"{self.name} needs at least {self.min_history} history period(s), got {len(history)}"
			)
		self._history = history
		self._fit(history)
		return self

	def forecast(self, horizon: int) -> list[float]:
		if self._history is None:
			raise ValueError("fit() must be called before forecast()")
		if horizon <= 0:
			return []
		return _clamp(self._forecast(int(horizon)))

	# subclasses implement these
	def _fit(self, history: list[float]):
		raise NotImplementedError

	def _forecast(self, horizon: int) -> list[float]:
		raise NotImplementedError


class NaiveForecast(BaseForecaster):
	name = NAIVE
	min_history = 1

	def _fit(self, history):
		self.fitted = [None, *history[:-1]]
		self._last = history[-1]

	def _forecast(self, horizon):
		return [self._last] * horizon


class SeasonalNaive(BaseForecaster):
	name = SEASONAL_NAIVE

	def __init__(self, season_length: int):
		super().__init__()
		if season_length < 1:
			raise ValueError("season_length must be >= 1")
		self.season_length = int(season_length)
		self.min_history = self.season_length
		self.params = {"season_length": self.season_length}

	def _fit(self, history):
		length = self.season_length
		self.fitted = [None] * min(length, len(history)) + [
			history[index - length] for index in range(length, len(history))
		]
		self._last_season = history[-length:]

	def _forecast(self, horizon):
		return [self._last_season[index % self.season_length] for index in range(horizon)]


class SingleExponentialSmoothing(BaseForecaster):
	name = EXPONENTIAL_SMOOTHING
	min_history = 2

	def __init__(self, alpha: float | None = None):
		super().__init__()
		self.alpha = alpha

	@staticmethod
	def _run(alpha: float, history: list[float]):
		level = history[0]
		fitted: list = [None]
		for value in history[1:]:
			fitted.append(level)
			level = alpha * value + (1.0 - alpha) * level
		return fitted, level

	def _fit(self, history):
		candidates = [self.alpha] if self.alpha is not None else PARAM_GRID
		best = None
		for alpha in candidates:
			fitted, level = self._run(alpha, history)
			mse = _mse(fitted, history)
			if best is None or mse < best[0]:
				best = (mse, alpha, fitted, level)

		_, alpha, fitted, level = best
		self.alpha = alpha
		self.params = {"alpha": alpha}
		self.fitted = fitted
		self._level = level

	def _forecast(self, horizon):
		return [self._level] * horizon


class HoltLinear(BaseForecaster):
	"""Holt's linear trend with optional damping (``phi < 1``)."""

	name = HOLT_LINEAR
	min_history = 3

	def __init__(self, alpha: float | None = None, beta: float | None = None, phi: float = 1.0):
		super().__init__()
		self.alpha = alpha
		self.beta = beta
		self.phi = phi

	@staticmethod
	def _run(alpha: float, beta: float, phi: float, history: list[float]):
		level = history[0]
		trend = history[1] - history[0]
		fitted: list = [None]
		for value in history[1:]:
			fitted.append(level + phi * trend)
			new_level = alpha * value + (1.0 - alpha) * (level + phi * trend)
			trend = beta * (new_level - level) + (1.0 - beta) * phi * trend
			level = new_level
		return fitted, level, trend

	def _fit(self, history):
		alphas = [self.alpha] if self.alpha is not None else PARAM_GRID
		betas = [self.beta] if self.beta is not None else PARAM_GRID
		best = None
		for alpha in alphas:
			for beta in betas:
				fitted, level, trend = self._run(alpha, beta, self.phi, history)
				mse = _mse(fitted, history)
				if best is None or mse < best[0]:
					best = (mse, alpha, beta, fitted, level, trend)

		_, alpha, beta, fitted, level, trend = best
		self.alpha, self.beta = alpha, beta
		self.params = {"alpha": alpha, "beta": beta, "phi": self.phi}
		self.fitted = fitted
		self._level, self._trend = level, trend

	def _forecast(self, horizon):
		forecasts = []
		damping_sum = 0.0
		for step in range(1, horizon + 1):
			damping_sum += self.phi**step
			forecasts.append(self._level + damping_sum * self._trend)
		return forecasts


class HoltWintersAdditive(BaseForecaster):
	"""Triple exponential smoothing, additive seasonality.

	Classic initialization: level = first-season average, per-period seasonal
	indices averaged over all complete seasons, trend from the difference of
	the first two seasons. Requires two full seasons of history.
	"""

	name = HOLT_WINTERS_ADDITIVE
	multiplicative = False

	def __init__(
		self,
		season_length: int,
		alpha: float | None = None,
		beta: float | None = None,
		gamma: float | None = None,
	):
		super().__init__()
		if season_length < 2:
			raise ValueError("season_length must be >= 2 for Holt-Winters")
		self.season_length = int(season_length)
		self.min_history = 2 * self.season_length
		self.alpha, self.beta, self.gamma = alpha, beta, gamma

	def _initial_state(self, history: list[float]):
		length = self.season_length
		n_seasons = len(history) // length

		season_averages = [
			sum(history[season * length : (season + 1) * length]) / length for season in range(n_seasons)
		]

		level = season_averages[0]
		trend = sum(history[length + index] - history[index] for index in range(length)) / (length * length)

		seasonals = []
		for index in range(length):
			terms = []
			for season in range(n_seasons):
				value = history[season * length + index]
				average = season_averages[season]
				if self.multiplicative:
					terms.append(value / average if average else 1.0)
				else:
					terms.append(value - average)
			seasonals.append(sum(terms) / n_seasons)

		return level, trend, seasonals

	def _run(self, alpha: float, beta: float, gamma: float, history: list[float]):
		level, trend, seasonals = self._initial_state(history)
		seasonals = list(seasonals)
		length = self.season_length
		fitted = []

		for index, value in enumerate(history):
			season_index = index % length
			seasonal = seasonals[season_index]

			if self.multiplicative:
				if not seasonal:
					seasonal = 1.0e-6
				fitted.append((level + trend) * seasonal)
				new_level = alpha * (value / seasonal) + (1.0 - alpha) * (level + trend)
				trend = beta * (new_level - level) + (1.0 - beta) * trend
				seasonals[season_index] = (
					gamma * (value / new_level if new_level else 1.0) + (1.0 - gamma) * seasonal
				)
			else:
				fitted.append(level + trend + seasonal)
				new_level = alpha * (value - seasonal) + (1.0 - alpha) * (level + trend)
				trend = beta * (new_level - level) + (1.0 - beta) * trend
				seasonals[season_index] = gamma * (value - new_level) + (1.0 - gamma) * seasonal

			level = new_level

		return fitted, level, trend, seasonals

	def _fit(self, history):
		alphas = [self.alpha] if self.alpha is not None else PARAM_GRID
		betas = [self.beta] if self.beta is not None else PARAM_GRID
		gammas = [self.gamma] if self.gamma is not None else PARAM_GRID

		best = None
		for alpha in alphas:
			for beta in betas:
				for gamma in gammas:
					fitted, level, trend, seasonals = self._run(alpha, beta, gamma, history)
					mse = _mse(fitted, history)
					if best is None or mse < best[0]:
						best = (mse, alpha, beta, gamma, fitted, level, trend, seasonals)

		_, alpha, beta, gamma, fitted, level, trend, seasonals = best
		self.alpha, self.beta, self.gamma = alpha, beta, gamma
		self.params = {
			"alpha": alpha,
			"beta": beta,
			"gamma": gamma,
			"season_length": self.season_length,
		}
		self.fitted = fitted
		self._level, self._trend, self._seasonals = level, trend, seasonals
		self._n = len(history)

	def _forecast(self, horizon):
		length = self.season_length
		forecasts = []
		for step in range(1, horizon + 1):
			seasonal = self._seasonals[(self._n + step - 1) % length]
			base = self._level + step * self._trend
			forecasts.append(base * seasonal if self.multiplicative else base + seasonal)
		return forecasts


class HoltWintersMultiplicative(HoltWintersAdditive):
	"""Multiplicative seasonality; falls back to additive when the history
	contains non-positive values (ratios to zero are undefined)."""

	name = HOLT_WINTERS_MULTIPLICATIVE
	multiplicative = True

	def _fit(self, history):
		if any(value <= 0 for value in history):
			self.multiplicative = False
			super()._fit(history)
			self.params["fallback"] = "additive"
			return
		super()._fit(history)


class CrostonForecast(BaseForecaster):
	"""Croston's method for intermittent demand.

	Separately smooths non-zero demand sizes (z) and inter-demand intervals
	(p); the per-period demand rate is z / p.
	"""

	name = CROSTON
	min_history = 2
	sba_correction = False

	def __init__(self, alpha: float | None = None):
		super().__init__()
		self.alpha = alpha

	def _rate(self, size: float, interval: float, alpha: float) -> float:
		rate = size / interval if interval else 0.0
		if self.sba_correction:
			rate *= 1.0 - alpha / 2.0
		return rate

	def _run(self, alpha: float, history: list[float]):
		size = None  # smoothed demand size
		interval = None  # smoothed inter-demand interval
		periods_since_demand = 1
		fitted = []

		for value in history:
			fitted.append(None if size is None else self._rate(size, interval, alpha))
			if value > 0:
				if size is None:
					size, interval = value, float(periods_since_demand)
				else:
					size = alpha * value + (1.0 - alpha) * size
					interval = alpha * periods_since_demand + (1.0 - alpha) * interval
				periods_since_demand = 1
			else:
				periods_since_demand += 1

		rate = 0.0 if size is None else self._rate(size, interval, alpha)
		return fitted, rate

	def _fit(self, history):
		candidates = [self.alpha] if self.alpha is not None else PARAM_GRID
		best = None
		for alpha in candidates:
			fitted, rate = self._run(alpha, history)
			mse = _mse(fitted, history)
			if best is None or mse < best[0]:
				best = (mse, alpha, fitted, rate)

		_, alpha, fitted, rate = best
		self.alpha = alpha
		self.params = {"alpha": alpha, "sba": self.sba_correction}
		self.fitted = fitted
		self._rate_value = rate

	def _forecast(self, horizon):
		return [self._rate_value] * horizon


class CrostonSBA(CrostonForecast):
	"""Croston with the Syntetos-Boylan Approximation bias correction:
	forecast = Croston rate x (1 - alpha / 2)."""

	name = CROSTON_SBA
	sba_correction = True


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------

SEASONAL_MODELS = (SEASONAL_NAIVE, HOLT_WINTERS_ADDITIVE, HOLT_WINTERS_MULTIPLICATIVE)

MODEL_REGISTRY = {
	NAIVE: NaiveForecast,
	SEASONAL_NAIVE: SeasonalNaive,
	EXPONENTIAL_SMOOTHING: SingleExponentialSmoothing,
	HOLT_LINEAR: HoltLinear,
	HOLT_WINTERS_ADDITIVE: HoltWintersAdditive,
	HOLT_WINTERS_MULTIPLICATIVE: HoltWintersMultiplicative,
	CROSTON: CrostonForecast,
	CROSTON_SBA: CrostonSBA,
}


def make_model(name: str, season_length: int | None = None) -> BaseForecaster:
	"""Instantiate a model by its registry name."""
	if name not in MODEL_REGISTRY:
		raise ValueError(f"Unknown forecast model: {name!r}")
	model_class = MODEL_REGISTRY[name]
	if name in SEASONAL_MODELS:
		if not season_length:
			raise ValueError(f"{name} requires a season_length")
		return model_class(season_length=int(season_length))
	return model_class()


def fit_model(model: BaseForecaster, history: list[float], horizon: int) -> ModelFit:
	"""Fit a model and package it as a ModelFit with in-sample metrics."""
	model.fit(history)
	forecast = model.forecast(horizon)

	errors = [
		predicted - actual
		for predicted, actual in zip(model.fitted, history, strict=True)
		if predicted is not None
	]
	metrics = {"source": "in-sample", "n_folds": 0}
	if errors:
		metrics["mad"] = sum(abs(error) for error in errors) / len(errors)
		metrics["rmse"] = (sum(error**2 for error in errors) / len(errors)) ** 0.5

	return ModelFit(
		model_name=model.name,
		params=dict(model.params),
		fitted=list(model.fitted),
		forecast=forecast,
		metrics=metrics,
	)
