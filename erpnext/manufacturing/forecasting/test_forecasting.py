# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure demand-forecasting engine.

``models.py``, ``engine.py`` and ``backtest.py`` have no frappe dependency,
but importing them through the ``erpnext`` package would pull in
``erpnext/__init__.py`` (which imports frappe). So when run as a plain file -

	python erpnext/manufacturing/forecasting/test_forecasting.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the package's absolute imports working without a site.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


try:
	from erpnext.manufacturing.forecasting import backtest, engine, models
except Exception:
	# stand-alone run: register stub packages so absolute imports of
	# erpnext.manufacturing.forecasting.* resolve without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.manufacturing", "erpnext.manufacturing.forecasting"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.manufacturing.forecasting.models", _HERE / "models.py")
	engine = _load_module("erpnext.manufacturing.forecasting.engine", _HERE / "engine.py")
	backtest = _load_module("erpnext.manufacturing.forecasting.backtest", _HERE / "backtest.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def seasonal_series(level=100.0, pattern=(30.0, -20.0, 10.0, -20.0), seasons=6, trend=0.0):
	series = []
	for season in range(seasons):
		for index, offset in enumerate(pattern):
			t = season * len(pattern) + index
			series.append(level + trend * t + offset)
	return series


INTERMITTENT = [0, 0, 4, 0, 0, 0, 6, 0, 0, 5, 0, 0, 0, 4, 0, 0, 6, 0, 0, 0]


class TestPeriodHelpers(unittest.TestCase):
	def test_period_start_monthly(self):
		self.assertEqual(models.period_start(D("2026-03-17"), models.MONTHLY), D("2026-03-01"))

	def test_period_start_weekly_is_monday(self):
		# 2026-03-19 is a Thursday; the Monday of that week is 2026-03-16
		self.assertEqual(models.period_start(D("2026-03-19"), models.WEEKLY), D("2026-03-16"))

	def test_add_periods_monthly_rollover(self):
		self.assertEqual(models.add_periods(D("2026-11-01"), 3, models.MONTHLY), D("2027-02-01"))
		self.assertEqual(models.add_periods(D("2026-01-31"), 1, models.MONTHLY), D("2026-02-28"))

	def test_period_range_zero_fill_length(self):
		periods = models.period_range(D("2026-01-15"), D("2026-06-02"), models.MONTHLY)
		self.assertEqual(periods, [D(f"2026-0{m}-01") for m in range(1, 7)])

	def test_timeseries_zero_ratio_and_value_at(self):
		ts = models.TimeSeries(
			periodicity=models.MONTHLY,
			points=[(D("2026-01-01"), 0.0), (D("2026-02-01"), 5.0), (D("2026-03-01"), 0.0)],
		)
		self.assertAlmostEqual(ts.zero_ratio(), 2 / 3)
		self.assertEqual(ts.value_at(D("2026-02-14")), 5.0)
		self.assertEqual(ts.value_at(D("2026-04-10")), 0.0)

	def test_to_forecast_points(self):
		points = models.to_forecast_points(D("2026-04-01"), [10.0, 20.0], models.MONTHLY)
		self.assertEqual([p.date for p in points], [D("2026-04-01"), D("2026-05-01")])
		self.assertEqual([p.qty for p in points], [10.0, 20.0])


class TestBaselineModels(unittest.TestCase):
	def test_naive_repeats_last_value(self):
		model = engine.NaiveForecast().fit([3.0, 7.0, 5.0])
		self.assertEqual(model.forecast(3), [5.0, 5.0, 5.0])
		self.assertEqual(model.fitted, [None, 3.0, 7.0])

	def test_seasonal_naive_repeats_last_season(self):
		history = [10.0, 20.0, 30.0, 40.0, 12.0, 22.0, 32.0, 42.0]
		model = engine.SeasonalNaive(season_length=4).fit(history)
		self.assertEqual(model.forecast(6), [12.0, 22.0, 32.0, 42.0, 12.0, 22.0])
		# fitted: first season unprimed, second season = first season values
		self.assertEqual(model.fitted[:4], [None] * 4)
		self.assertEqual(model.fitted[4:], [10.0, 20.0, 30.0, 40.0])

	def test_ses_on_constant_series(self):
		model = engine.SingleExponentialSmoothing().fit([50.0] * 12)
		for value in model.forecast(4):
			self.assertAlmostEqual(value, 50.0)

	def test_ses_explicit_alpha_is_kept(self):
		model = engine.SingleExponentialSmoothing(alpha=0.5).fit([10.0, 20.0, 30.0])
		self.assertEqual(model.params["alpha"], 0.5)
		# level: 10 -> 15 -> 22.5
		self.assertAlmostEqual(model.forecast(1)[0], 22.5)


class TestTrendAndSeasonalModels(unittest.TestCase):
	def test_holt_linear_extends_trend(self):
		history = [10.0 + 2.0 * t for t in range(12)]  # 10, 12, ..., 32
		model = engine.HoltLinear().fit(history)
		forecast = model.forecast(3)
		for step, value in enumerate(forecast, start=1):
			self.assertAlmostEqual(value, 32.0 + 2.0 * step, delta=0.5)

	def test_holt_damped_flattens(self):
		history = [10.0 + 2.0 * t for t in range(12)]
		damped = engine.HoltLinear(alpha=0.5, beta=0.3, phi=0.8).fit(history).forecast(10)
		undamped = engine.HoltLinear(alpha=0.5, beta=0.3, phi=1.0).fit(history).forecast(10)
		self.assertLess(damped[-1], undamped[-1])

	def test_hw_additive_recovers_seasonal_pattern(self):
		pattern = (30.0, -20.0, 10.0, -20.0)
		history = seasonal_series(level=100.0, pattern=pattern, seasons=6)
		model = engine.HoltWintersAdditive(season_length=4).fit(history)
		forecast = model.forecast(4)
		expected = [100.0 + offset for offset in pattern]
		for got, want in zip(forecast, expected, strict=True):
			self.assertAlmostEqual(got, want, delta=3.0)

	def test_hw_additive_with_trend(self):
		pattern = (15.0, -15.0, 5.0, -5.0)
		history = seasonal_series(level=100.0, pattern=pattern, seasons=6, trend=1.0)
		model = engine.HoltWintersAdditive(season_length=4).fit(history)
		forecast = model.forecast(4)
		n = len(history)
		for step, got in enumerate(forecast, start=1):
			t = n + step - 1
			want = 100.0 + 1.0 * t + pattern[t % 4]
			self.assertAlmostEqual(got, want, delta=4.0)

	def test_hw_multiplicative_on_positive_seasonal_series(self):
		factors = (1.3, 0.8, 1.1, 0.8)
		history = []
		for _season in range(6):
			for factor in factors:
				history.append(100.0 * factor)
		model = engine.HoltWintersMultiplicative(season_length=4).fit(history)
		self.assertNotIn("fallback", model.params)
		forecast = model.forecast(4)
		for got, factor in zip(forecast, factors, strict=True):
			self.assertAlmostEqual(got, 100.0 * factor, delta=5.0)

	def test_hw_multiplicative_falls_back_to_additive_on_zeros(self):
		history = seasonal_series(level=10.0, pattern=(10.0, -10.0, 5.0, -5.0), seasons=3)
		history[2] = 0.0  # introduce a zero
		model = engine.HoltWintersMultiplicative(season_length=4).fit(history)
		self.assertEqual(model.params.get("fallback"), "additive")
		for value in model.forecast(4):
			self.assertGreaterEqual(value, 0.0)
			self.assertLess(value, 1000.0)

	def test_hw_requires_two_seasons(self):
		with self.assertRaises(ValueError):
			engine.HoltWintersAdditive(season_length=4).fit([1.0] * 7)


class TestClamping(unittest.TestCase):
	def test_negative_forecasts_clamp_to_zero(self):
		history = [100.0 - 12.0 * t for t in range(10)]  # hits negative territory fast
		forecast = engine.HoltLinear(alpha=0.9, beta=0.9).fit(history).forecast(12)
		self.assertEqual(forecast[-1], 0.0)
		for value in forecast:
			self.assertGreaterEqual(value, 0.0)


class TestIntermittentModels(unittest.TestCase):
	def test_croston_rate_on_regular_intermittent_demand(self):
		# demand of 4 every 4th period -> rate around 1.0
		history = [0.0, 0.0, 0.0, 4.0] * 5
		model = engine.CrostonForecast(alpha=0.1).fit(history)
		rate = model.forecast(1)[0]
		self.assertAlmostEqual(rate, 1.0, delta=0.15)

	def test_sba_shrinks_croston(self):
		history = [float(x) for x in INTERMITTENT]
		croston = engine.CrostonForecast(alpha=0.3).fit(history).forecast(1)[0]
		sba = engine.CrostonSBA(alpha=0.3).fit(history).forecast(1)[0]
		self.assertGreater(croston, 0.0)
		self.assertAlmostEqual(sba, croston * (1.0 - 0.3 / 2.0), places=9)

	def test_croston_all_zero_history_forecasts_zero(self):
		model = engine.CrostonForecast().fit([0.0] * 8)
		self.assertEqual(model.forecast(3), [0.0, 0.0, 0.0])


class TestEngineFactory(unittest.TestCase):
	def test_make_model_all_names(self):
		for name in engine.MODEL_REGISTRY:
			model = engine.make_model(name, season_length=4)
			self.assertEqual(model.name, name)

	def test_make_model_unknown_name(self):
		with self.assertRaises(ValueError):
			engine.make_model("Prophet")

	def test_seasonal_model_requires_season_length(self):
		with self.assertRaises(ValueError):
			engine.make_model(engine.SEASONAL_NAIVE)

	def test_forecast_before_fit_raises(self):
		with self.assertRaises(ValueError):
			engine.NaiveForecast().forecast(3)


class TestBacktest(unittest.TestCase):
	def test_fold_count_matches_holdout(self):
		history = [float(10 + t) for t in range(20)]
		result = backtest.rolling_origin(history, engine.NAIVE, holdout=4)
		self.assertEqual(result.n_folds, 4)

	def test_fold_count_skips_infeasible_folds(self):
		# HW needs 2*4=8 training periods; with 10 points and holdout 4 the
		# first two origins (train lengths 6, 7) are skipped
		history = seasonal_series(pattern=(5.0, -5.0), seasons=5)  # 10 points, L=2
		result = backtest.rolling_origin(history, engine.HOLT_WINTERS_ADDITIVE, season_length=4, holdout=4)
		self.assertEqual(result.n_folds, 2)

	def test_perfect_model_has_zero_mape(self):
		history = [10.0, 20.0, 30.0, 40.0] * 5
		result = backtest.rolling_origin(history, engine.SEASONAL_NAIVE, season_length=4, holdout=4)
		self.assertAlmostEqual(result.mape, 0.0)
		self.assertAlmostEqual(result.bias, 0.0)

	def test_metrics_shape(self):
		history = [float(t % 7 + 1) for t in range(24)]
		result = backtest.rolling_origin(history, engine.EXPONENTIAL_SMOOTHING, holdout=6)
		self.assertEqual(result.n_folds, 6)
		for metric in (result.mape, result.bias, result.mad, result.rmse):
			self.assertIsNotNone(metric)
		self.assertGreaterEqual(result.mape, 0.0)

	def test_all_zero_holdout_uses_wmape_fallback(self):
		history = [5.0, 5.0, 5.0, 5.0, 0.0, 0.0, 0.0]
		result = backtest.rolling_origin(history, engine.NAIVE, holdout=3)
		# naive predicts 5 then decays as zeros arrive; actuals all zero,
		# scale = 5 -> MAPE is MAD-based, finite and positive
		self.assertIsNotNone(result.mape)
		self.assertGreater(result.mape, 0.0)


class TestChampionSelection(unittest.TestCase):
	def test_prefers_seasonal_model_on_seasonal_data(self):
		history = seasonal_series(level=100.0, pattern=(40.0, -30.0, 10.0, -20.0), seasons=6)
		fit, results = backtest.select_champion(history, season_length=4, horizon=4)
		self.assertIn(fit.model_name, engine.SEASONAL_MODELS)
		self.assertTrue(results)
		self.assertEqual(fit.metrics["source"], "backtest")
		self.assertEqual(len(fit.forecast), 4)

	def test_prefers_intermittent_model_on_intermittent_data(self):
		history = [float(x) for x in INTERMITTENT]
		fit, results = backtest.select_champion(history, season_length=4, horizon=3)
		allowed = set(backtest.INTERMITTENT_CANDIDATES)
		self.assertIn(fit.model_name, allowed)
		for result in results:
			self.assertIn(result.model_name, allowed)

	def test_champion_metrics_match_backtest_result(self):
		history = seasonal_series(seasons=6)
		fit, results = backtest.select_champion(history, season_length=4, horizon=2)
		winning = next(result for result in results if result.model_name == fit.model_name)
		self.assertEqual(fit.metrics["mape"], winning.mape)
		self.assertEqual(fit.metrics["n_folds"], winning.n_folds)

	def test_zero_history_raises(self):
		with self.assertRaises(ValueError):
			backtest.select_champion([], season_length=4)

	def test_short_history_degrades_gracefully(self):
		fit, _results = backtest.select_champion([7.0, 9.0, 8.0], season_length=12, horizon=2)
		self.assertEqual(len(fit.forecast), 2)
		self.assertIsNotNone(fit.model_name)
		# single point of history: still no crash
		fit_one, _ = backtest.select_champion([5.0], season_length=12, horizon=2)
		self.assertEqual(fit_one.model_name, engine.NAIVE)
		self.assertEqual(fit_one.forecast, [5.0, 5.0])

	def test_explicit_candidates_are_respected(self):
		history = seasonal_series(seasons=6)
		fit, results = backtest.select_champion(
			history, season_length=4, candidate_models=[engine.NAIVE], horizon=1
		)
		self.assertEqual(fit.model_name, engine.NAIVE)
		self.assertEqual([result.model_name for result in results], [engine.NAIVE])


class TestNamedModelFit(unittest.TestCase):
	def test_named_seasonal_model_falls_back_on_short_history(self):
		history = [10.0, 12.0, 11.0, 13.0, 12.0]  # < 2 seasons of 12
		fit = backtest.fit_named_model(history, engine.HOLT_WINTERS_ADDITIVE, season_length=12, horizon=3)
		self.assertIn(fit.model_name, (engine.HOLT_LINEAR, engine.EXPONENTIAL_SMOOTHING, engine.NAIVE))
		self.assertEqual(len(fit.forecast), 3)

	def test_named_model_used_when_history_sufficient(self):
		history = seasonal_series(seasons=6)
		fit = backtest.fit_named_model(history, engine.HOLT_WINTERS_ADDITIVE, season_length=4, horizon=4)
		self.assertEqual(fit.model_name, engine.HOLT_WINTERS_ADDITIVE)
		self.assertEqual(fit.metrics["source"], "backtest")

	def test_empty_history_raises(self):
		with self.assertRaises(ValueError):
			backtest.fit_named_model([], engine.NAIVE, horizon=1)


if __name__ == "__main__":
	unittest.main(verbosity=2)
