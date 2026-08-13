# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Rolling-origin backtesting and champion-model selection (pure).

Evaluation protocol: for each of the last ``holdout`` periods, fit the model
on the expanding window of everything before that period and score its
one-step-ahead forecast against the actual. Metrics:

- ``mape``  mean absolute percentage error over folds with non-zero actuals
  (percent). If every holdout actual is zero, falls back to a MAD-based
  WMAPE: 100 * MAD / mean(non-zero |history|).
- ``bias``  mean error / mean actual (fraction; error = forecast - actual,
  positive = over-forecasting).
- ``mad`` / ``rmse``  in units of demand.

``select_champion`` picks the lowest-MAPE model with a stability tiebreak
(lower |bias|). Series where more than 40% of periods are zero-demand are
classified intermittent and restricted to Croston / Croston SBA / Naive.
"""

from __future__ import annotations

from erpnext.manufacturing.forecasting import engine
from erpnext.manufacturing.forecasting.models import BacktestResult, ModelFit

INTERMITTENCY_THRESHOLD = 0.4

DEFAULT_CANDIDATES = (
	engine.NAIVE,
	engine.SEASONAL_NAIVE,
	engine.EXPONENTIAL_SMOOTHING,
	engine.HOLT_LINEAR,
	engine.HOLT_WINTERS_ADDITIVE,
	engine.HOLT_WINTERS_MULTIPLICATIVE,
)

INTERMITTENT_CANDIDATES = (
	engine.CROSTON,
	engine.CROSTON_SBA,
	engine.NAIVE,
)

# graceful degradation for named seasonal models on short histories
FALLBACK_MODEL = {
	engine.HOLT_WINTERS_ADDITIVE: engine.HOLT_LINEAR,
	engine.HOLT_WINTERS_MULTIPLICATIVE: engine.HOLT_LINEAR,
	engine.SEASONAL_NAIVE: engine.EXPONENTIAL_SMOOTHING,
	engine.HOLT_LINEAR: engine.EXPONENTIAL_SMOOTHING,
	engine.EXPONENTIAL_SMOOTHING: engine.NAIVE,
	engine.CROSTON: engine.NAIVE,
	engine.CROSTON_SBA: engine.NAIVE,
}


def is_intermittent(history: list[float]) -> bool:
	if not history:
		return False
	zeros = sum(1 for value in history if not value)
	return zeros / len(history) > INTERMITTENCY_THRESHOLD


def default_holdout(n_periods: int) -> int:
	"""Number of rolling-origin folds: about a quarter of the history,
	between 2 and 8, always leaving at least 2 training periods."""
	holdout = max(2, min(8, n_periods // 4))
	return max(1, min(holdout, n_periods - 2))


def compute_metrics(actuals: list[float], predictions: list[float], history_scale: float = 0.0) -> dict:
	"""MAPE / bias / MAD / RMSE for paired actual-prediction folds.

	``history_scale`` (mean of the non-zero absolute history) anchors the
	MAD-based WMAPE fallback when every holdout actual is zero.
	"""
	errors = [predicted - actual for predicted, actual in zip(predictions, actuals, strict=True)]
	n = len(errors)
	if not n:
		return {"mape": None, "bias": None, "mad": None, "rmse": None}

	mad = sum(abs(error) for error in errors) / n
	rmse = (sum(error**2 for error in errors) / n) ** 0.5

	percentage_errors = [
		abs(error) / abs(actual) for error, actual in zip(errors, actuals, strict=True) if actual
	]
	if percentage_errors:
		mape = 100.0 * sum(percentage_errors) / len(percentage_errors)
	elif history_scale:
		# every holdout actual was zero — MAD-based WMAPE fallback
		mape = 100.0 * mad / history_scale
	else:
		mape = 0.0 if not mad else None

	mean_actual = sum(actuals) / n
	mean_error = sum(errors) / n
	bias = (mean_error / mean_actual) if mean_actual else (0.0 if not mean_error else None)

	return {"mape": mape, "bias": bias, "mad": mad, "rmse": rmse}


def _history_scale(history: list[float]) -> float:
	non_zero = [abs(value) for value in history if value]
	return sum(non_zero) / len(non_zero) if non_zero else 0.0


def rolling_origin(
	history: list[float],
	model_name: str,
	season_length: int | None = None,
	holdout: int | None = None,
) -> BacktestResult:
	"""Expanding-window one-step-ahead evaluation of one model.

	Folds whose training window is too short for the model are skipped;
	``n_folds`` reports the folds actually scored.
	"""
	n = len(history)
	if holdout is None:
		holdout = default_holdout(n)
	holdout = max(0, min(holdout, n - 1))

	actuals, predictions = [], []
	for origin in range(n - holdout, n):
		train = history[:origin]
		try:
			model = engine.make_model(model_name, season_length)
			model.fit(train)
		except ValueError:
			continue
		predictions.append(model.forecast(1)[0])
		actuals.append(history[origin])

	metrics = compute_metrics(actuals, predictions, _history_scale(history))
	return BacktestResult(
		model_name=model_name,
		mape=metrics["mape"],
		bias=metrics["bias"],
		mad=metrics["mad"],
		rmse=metrics["rmse"],
		n_folds=len(actuals),
	)


def _refit(model_name: str, history: list[float], season_length: int | None, horizon: int) -> ModelFit:
	model = engine.make_model(model_name, season_length)
	return engine.fit_model(model, history, horizon)


def resolve_model_name(model_name: str, history_length: int, season_length: int | None) -> str:
	"""Degrade a requested model to something the history can support.

	Seasonal models need two full seasons; trend/smoothing models need their
	own minimum history. Falls back along FALLBACK_MODEL until feasible.
	"""
	name = model_name
	while True:
		if name in engine.SEASONAL_MODELS:
			needed = 2 * (season_length or 0) if name != engine.SEASONAL_NAIVE else (season_length or 0)
			# Seasonal Naive can technically run on one season, but two are
			# needed for any meaningful backtest — require min_history only.
			model_class_min = engine.MODEL_REGISTRY[name](season_length or 2).min_history
			needed = max(needed, model_class_min)
		else:
			model_class_min = engine.MODEL_REGISTRY[name]().min_history
			needed = model_class_min

		if history_length >= needed or name == engine.NAIVE:
			return name
		name = FALLBACK_MODEL.get(name, engine.NAIVE)


def fit_named_model(
	history: list[float],
	model_name: str,
	season_length: int | None = None,
	horizon: int = 1,
	holdout: int | None = None,
) -> ModelFit:
	"""Fit one explicitly requested model (degrading gracefully on short
	history) and attach backtest metrics when a backtest is possible."""
	if not history:
		raise ValueError("Cannot fit a forecast model on empty history")

	resolved = resolve_model_name(model_name, len(history), season_length)
	fit = _refit(resolved, history, season_length, horizon)

	result = rolling_origin(history, resolved, season_length, holdout)
	if result.n_folds:
		fit.metrics = {
			"source": "backtest",
			"mape": result.mape,
			"bias": result.bias,
			"mad": result.mad,
			"rmse": result.rmse,
			"n_folds": result.n_folds,
		}
	return fit


def select_champion(
	history: list[float],
	season_length: int | None = None,
	candidate_models: list[str] | None = None,
	horizon: int = 1,
	holdout: int | None = None,
) -> tuple[ModelFit, list[BacktestResult]]:
	"""Backtest all candidates and refit the winner on the full history.

	Returns ``(champion ModelFit, all BacktestResults)``. Champion = lowest
	MAPE, ties broken by lower |bias|. Intermittent series (> 40% zero
	periods) are auto-restricted to Croston / Croston SBA / Naive unless the
	caller pins ``candidate_models`` explicitly.
	"""
	if not history:
		raise ValueError("Cannot select a forecast model on empty history")

	if candidate_models is None:
		candidate_models = list(INTERMITTENT_CANDIDATES if is_intermittent(history) else DEFAULT_CANDIDATES)

	results = []
	for name in candidate_models:
		if name in engine.SEASONAL_MODELS and not season_length:
			continue
		result = rolling_origin(history, name, season_length, holdout)
		if result.n_folds:
			results.append(result)

	if not results:
		# history too short to backtest anything — degrade to the best
		# feasible named model with in-sample metrics
		fit = fit_named_model(history, engine.NAIVE, season_length, horizon, holdout)
		return fit, []

	def sort_key(result: BacktestResult):
		mape = result.mape if result.mape is not None else float("inf")
		bias = abs(result.bias) if result.bias is not None else float("inf")
		return (mape, bias, result.model_name)

	best = min(results, key=sort_key)
	fit = _refit(best.model_name, history, season_length, horizon)
	fit.metrics = {
		"source": "backtest",
		"mape": best.mape,
		"bias": best.bias,
		"mad": best.mad,
		"rmse": best.rmse,
		"n_folds": best.n_folds,
	}
	return fit, results
