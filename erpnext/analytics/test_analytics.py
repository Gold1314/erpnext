# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure semantic metric layer (models + engine + registry).

``models.py``, ``engine.py`` and ``registry.py`` have no frappe dependency,
but importing them through the ``erpnext`` package would pull in
``erpnext/__init__.py`` (which imports frappe). So when run as a plain file -

	python erpnext/analytics/test_analytics.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.analytics.models import ...``
working without a site. Same bootstrap as
``erpnext/accounts/forecasting/test_engine.py``.
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
	from erpnext.analytics import engine, models, registry
except Exception:
	for _pkg in ("erpnext", "erpnext.analytics"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.analytics.models", _HERE / "models.py")
	engine = _load_module("erpnext.analytics.engine", _HERE / "engine.py")
	registry = _load_module("erpnext.analytics.registry", _HERE / "registry.py")


PERIOD_START = datetime.date(2026, 1, 1)
PERIOD_END = datetime.date(2026, 1, 31)

#: one coherent set of inputs covering every name the pack declares.
#: Expected values below are hand-computed from exactly these numbers.
INPUTS = {
	# balance sheet, as of period end
	"current_assets": 500000.0,
	"current_liabilities": 250000.0,
	"quick_assets": 200000.0,
	"cash_and_bank": 125000.456,
	"ar_balance": 200000.0,
	"ap_balance": 100000.0,
	"inventory_balance": 160000.0,
	"average_inventory": 155000.0,
	"opening_ar_balance": 150000.0,
	# receivable ageing
	"ar_total_outstanding": 180000.0,
	"ar_overdue_outstanding": 45000.0,
	"ar_current_outstanding": 135000.0,
	# profit and loss, for the period
	"revenue": 1000000.0,
	"cogs": 600000.0,
	"opex": 250000.0,
	"operating_expense": 220000.0,
	"prior_revenue": 800000.0,
	# operations
	"otif_lines": 171.0,
	"delivered_lines": 180.0,
	"ppv_variance_amount": 4500.0,
	"ppv_baseline_amount": 300000.0,
	# governance
	"close_cycle_days_total": 18.0,
	"close_cycles_completed": 4.0,
	"open_high_anomalies": 3.0,
	"open_sod_violations": 2.0,
	"forecast_mape_sum": 63.0,
	"forecast_mape_count": 3.0,
}

#: hand-computed expectation per metric key, at the unit's precision
EXPECTED = {
	# 500000 / 250000
	"current_ratio": 2.0,
	# 200000 / 250000
	"quick_ratio": 0.8,
	# 500000 - 250000
	"working_capital": 250000.0,
	# rounded to 2dp
	"cash_balance": 125000.46,
	# 365 * 200000 / 1000000
	"dso": 73.0,
	# 365 * 100000 / 600000 = 60.8333..
	"dpo": 60.8,
	# 365 * 160000 / 600000 = 97.3333..
	"dio": 97.3,
	# 73 + 97.3333.. - 60.8333.. = 109.5
	"cash_conversion_cycle": 109.5,
	# 100 * 45000 / 180000
	"ar_overdue_pct": 25.0,
	# 100 * (150000 + 1000000 - 180000) / (150000 + 1000000 - 135000)
	# = 100 * 970000 / 1015000 = 95.5665..
	"collection_effectiveness_index": 95.57,
	"revenue": 1000000.0,
	"cogs": 600000.0,
	"opex": 250000.0,
	# 100 * (1000000 - 600000) / 1000000
	"gross_margin_pct": 40.0,
	# 100 * (1000000 - 600000 - 220000) / 1000000
	"operating_margin_pct": 18.0,
	# 100 * (1000000 - 600000 - 250000) / 1000000
	"net_margin_pct": 15.0,
	# 100 * (1000000 - 800000) / 800000
	"revenue_growth_pct": 25.0,
	# 600000 / 155000 = 3.87096..
	"inventory_turns": 3.871,
	# 100 * 171 / 180
	"otif_pct": 95.0,
	# 100 * 4500 / 300000
	"purchase_price_variance_pct": 1.5,
	# 18 / 4
	"close_duration_days": 4.5,
	"open_high_anomalies": 3,
	"sod_open_violations": 2,
	# 63 / 3
	"forecast_accuracy_mape": 21.0,
}


def spec(key):
	return registry.METRICS[key]


class TestRegistryIntegrity(unittest.TestCase):
	def test_registry_is_structurally_valid(self):
		self.assertEqual(registry.validate_registry(), [])

	def test_keys_are_unique(self):
		keys = [s.key for s in registry.ALL_SPECS]
		self.assertEqual(len(keys), len(set(keys)))

	def test_pack_size_and_categories(self):
		self.assertGreaterEqual(len(registry.ALL_SPECS), 22)
		covered = {s.category for s in registry.ALL_SPECS}
		self.assertEqual(covered, set(models.CATEGORIES))

	def test_units_grains_directions_are_valid(self):
		for s in registry.ALL_SPECS:
			self.assertIn(s.unit, models.UNITS, s.key)
			self.assertIn(s.grain, models.GRAINS, s.key)
			self.assertIn(s.direction, models.DIRECTIONS, s.key)
			self.assertTrue(s.formula_text.strip(), s.key)
			self.assertTrue(s.description.strip(), s.key)
			self.assertTrue(s.inputs, s.key)

	def test_every_input_the_arithmetic_touches_is_declared(self):
		"""A metric reading an undeclared input would be caught by the engine
		as a failed computation - assert none of them are."""
		for s in registry.ALL_SPECS:
			value = engine.compute_metric(s, {name: 1.0 for name in s.inputs})
			for warning in value.warnings:
				self.assertNotIn("computation failed", warning, s.key)
				self.assertNotIn("unavailable", warning, s.key)

	def test_default_thresholds_point_at_their_own_metric(self):
		for s in registry.ALL_SPECS:
			if s.default_threshold:
				self.assertEqual(s.default_threshold.key, s.key)
				self.assertNotEqual(s.direction, models.NEUTRAL, s.key)

	def test_all_inputs_is_the_union_of_declared_inputs(self):
		self.assertEqual(set(registry.all_inputs()), {n for s in registry.ALL_SPECS for n in s.inputs})

	def test_test_fixture_covers_every_declared_input(self):
		self.assertEqual(set(registry.all_inputs()) - set(INPUTS), set())

	def test_catalog_as_dicts_is_serializable(self):
		import json

		payload = registry.catalog_as_dicts()
		self.assertEqual(len(payload), len(registry.ALL_SPECS))
		json.dumps(payload)  # must not raise
		self.assertNotIn("fn", payload[0])

	def test_get_specs_filters_and_skips_unknown(self):
		selected = registry.get_specs(["dso", "not_a_metric", "current_ratio"])
		self.assertEqual([s.key for s in selected], ["dso", "current_ratio"])


class TestMetricArithmetic(unittest.TestCase):
	def test_every_metric_matches_its_hand_computed_value(self):
		for key, expected in EXPECTED.items():
			with self.subTest(metric=key):
				value = engine.compute_metric(spec(key), INPUTS, "Wind Power LLC", PERIOD_START, PERIOD_END)
				self.assertEqual(value.value, expected)
				self.assertEqual(value.warnings, [])
				self.assertEqual(value.company, "Wind Power LLC")
				self.assertEqual(value.period_start, PERIOD_START)
				self.assertEqual(value.period_end, PERIOD_END)

	def test_expectations_cover_the_whole_pack(self):
		self.assertEqual(set(EXPECTED), set(registry.METRICS))

	def test_inputs_used_records_only_declared_inputs(self):
		value = engine.compute_metric(spec("dso"), INPUTS)
		self.assertEqual(value.inputs_used, {"ar_balance": 200000.0, "revenue": 1000000.0})

	def test_count_metrics_are_integers(self):
		value = engine.compute_metric(spec("open_high_anomalies"), {"open_high_anomalies": 3.0})
		self.assertIsInstance(value.value, int)

	def test_negative_and_zero_numerators_are_fine(self):
		value = engine.compute_metric(spec("gross_margin_pct"), {"revenue": 100.0, "cogs": 150.0})
		self.assertEqual(value.value, -50.0)
		self.assertEqual(value.warnings, [])

	def test_ccc_is_none_when_any_component_is_undefined(self):
		inputs = dict(INPUTS, cogs=0.0)
		value = engine.compute_metric(spec("cash_conversion_cycle"), inputs)
		self.assertIsNone(value.value)
		self.assertTrue(value.warnings)

	def test_cei_baseline_of_zero_is_guarded(self):
		inputs = {
			"opening_ar_balance": 0.0,
			"revenue": 0.0,
			"ar_total_outstanding": 0.0,
			"ar_current_outstanding": 0.0,
		}
		value = engine.compute_metric(spec("collection_effectiveness_index"), inputs)
		self.assertIsNone(value.value)


class TestGuards(unittest.TestCase):
	def test_zero_inputs_never_raise_for_any_metric(self):
		zeros = dict.fromkeys(registry.all_inputs(), 0.0)
		for s in registry.ALL_SPECS:
			with self.subTest(metric=s.key):
				value = engine.compute_metric(s, zeros)  # must not raise
				if "/" in s.formula_text:
					self.assertIsNone(value.value, s.key)
					self.assertTrue(value.warnings, s.key)
					self.assertTrue(
						any("zero" in w for w in value.warnings),
						f"{s.key}: expected a divide-by-zero warning, got {value.warnings}",
					)
				else:
					self.assertIsNotNone(value.value, s.key)
					self.assertEqual(value.warnings, [], s.key)

	def test_missing_input_makes_the_metric_unavailable(self):
		"""A cold feed must not report a falsely-good zero."""
		value = engine.compute_metric(spec("dso"), {"revenue": 1000.0})
		self.assertIsNone(value.value)
		self.assertTrue(any("ar_balance" in w and "unavailable" in w for w in value.warnings))
		self.assertEqual(value.inputs_used["ar_balance"], 0.0)

	def test_none_input_is_treated_as_missing(self):
		value = engine.compute_metric(spec("dso"), {"ar_balance": None, "revenue": 1000.0})
		self.assertIsNone(value.value)
		self.assertTrue(any("unavailable" in w for w in value.warnings))

	def test_non_numeric_input_makes_the_metric_unavailable(self):
		value = engine.compute_metric(spec("dso"), {"ar_balance": "oops", "revenue": 1000.0})
		self.assertIsNone(value.value)
		self.assertTrue(any("not numeric" in w for w in value.warnings))

	def test_a_real_zero_still_computes(self):
		"""An unused module legitimately reporting 0 is not the same as missing."""
		value = engine.compute_metric(spec("open_high_anomalies"), {"open_high_anomalies": 0.0})
		self.assertEqual(value.value, 0)
		self.assertEqual(value.warnings, [])

	def test_unavailable_input_never_grades_green(self):
		value = engine.compute_metric(spec("dso"), {"revenue": 1000.0})
		self.assertEqual(
			engine.evaluate_threshold(spec("dso"), value.value, spec("dso").default_threshold),
			models.UNKNOWN,
		)

	def test_numeric_string_input_is_accepted(self):
		value = engine.compute_metric(spec("dso"), {"ar_balance": "200000", "revenue": "1000000"})
		self.assertEqual(value.value, 73.0)
		self.assertEqual(value.warnings, [])

	def test_exploding_formula_is_caught(self):
		bad = models.MetricSpec(
			key="bad_metric",
			label="Bad",
			unit=models.RATIO,
			grain=models.GRAIN_COMPANY,
			direction=models.NEUTRAL,
			category=models.LIQUIDITY,
			description="raises",
			formula_text="boom",
			inputs=["a"],
			fn=lambda v, c: 1 / 0,
		)
		value = engine.compute_metric(bad, {"a": 1.0})
		self.assertIsNone(value.value)
		self.assertTrue(any("division by zero" in w for w in value.warnings))

	def test_spec_without_arithmetic_is_reported(self):
		bare = models.MetricSpec(
			key="bare",
			label="Bare",
			unit=models.RATIO,
			grain=models.GRAIN_COMPANY,
			direction=models.NEUTRAL,
			category=models.LIQUIDITY,
			description="no fn",
			formula_text="n/a",
			inputs=["a"],
		)
		value = engine.compute_metric(bare, {"a": 1.0})
		self.assertIsNone(value.value)
		self.assertIn("bare: no arithmetic defined", value.warnings)

	def test_compute_many_uses_shared_pool_and_overrides(self):
		specs = registry.get_specs(["dso", "current_ratio"])
		values = engine.compute_many(
			specs,
			inputs_by_key={"dso": {"ar_balance": 100000.0, "revenue": 1000000.0}},
			shared_inputs=INPUTS,
			company="Wind Power LLC",
		)
		self.assertEqual([v.key for v in values], ["dso", "current_ratio"])
		self.assertEqual(values[0].value, 36.5)
		self.assertEqual(values[1].value, 2.0)
		self.assertEqual(values[1].company, "Wind Power LLC")


class TestRounding(unittest.TestCase):
	def test_precision_per_unit(self):
		self.assertEqual(engine.round_for_unit(1234.5678, models.CURRENCY), 1234.57)
		self.assertEqual(engine.round_for_unit(12.3456, models.PERCENT), 12.35)
		self.assertEqual(engine.round_for_unit(45.678, models.DAYS), 45.7)
		self.assertEqual(engine.round_for_unit(1.23456, models.RATIO), 1.235)
		self.assertEqual(engine.round_for_unit(3.7, models.COUNT), 4)
		self.assertIsInstance(engine.round_for_unit(3.7, models.COUNT), int)
		self.assertIsNone(engine.round_for_unit(None, models.CURRENCY))

	def test_unit_precision_table_covers_every_unit(self):
		self.assertEqual(set(models.UNIT_PRECISION), set(models.UNITS))


class TestThresholds(unittest.TestCase):
	def test_higher_is_better_bands(self):
		s = spec("current_ratio")
		t = models.Threshold("current_ratio", green_min=2.0, amber_min=1.2)
		self.assertEqual(engine.evaluate_threshold(s, 2.5, t), models.GREEN)
		self.assertEqual(engine.evaluate_threshold(s, 2.0, t), models.GREEN)
		self.assertEqual(engine.evaluate_threshold(s, 1.5, t), models.AMBER)
		self.assertEqual(engine.evaluate_threshold(s, 1.2, t), models.AMBER)
		self.assertEqual(engine.evaluate_threshold(s, 1.0, t), models.RED)

	def test_lower_is_better_bands(self):
		s = spec("dso")
		t = models.Threshold("dso", green_min=45.0, amber_min=60.0)
		self.assertEqual(engine.evaluate_threshold(s, 30.0, t), models.GREEN)
		self.assertEqual(engine.evaluate_threshold(s, 45.0, t), models.GREEN)
		self.assertEqual(engine.evaluate_threshold(s, 55.0, t), models.AMBER)
		self.assertEqual(engine.evaluate_threshold(s, 60.0, t), models.AMBER)
		self.assertEqual(engine.evaluate_threshold(s, 75.0, t), models.RED)

	def test_red_max_is_a_hard_breach_in_both_directions(self):
		lower = spec("dso")
		t_lower = models.Threshold("dso", green_min=45.0, amber_min=60.0, red_max=90.0)
		self.assertEqual(engine.evaluate_threshold(lower, 95.0, t_lower), models.RED)
		self.assertEqual(engine.evaluate_threshold(lower, 50.0, t_lower), models.AMBER)

		higher = spec("current_ratio")
		t_higher = models.Threshold("current_ratio", green_min=2.0, amber_min=1.2, red_max=1.0)
		self.assertEqual(engine.evaluate_threshold(higher, 0.5, t_higher), models.RED)
		self.assertEqual(engine.evaluate_threshold(higher, 2.5, t_higher), models.GREEN)

	def test_red_max_alone_means_green_until_breached(self):
		s = spec("working_capital")
		t = s.default_threshold
		self.assertEqual(engine.evaluate_threshold(s, -1.0, t), models.RED)
		self.assertEqual(engine.evaluate_threshold(s, 0.0, t), models.GREEN)
		self.assertEqual(engine.evaluate_threshold(s, 100000.0, t), models.GREEN)

	def test_unknown_cases(self):
		s = spec("current_ratio")
		t = models.Threshold("current_ratio", green_min=2.0)
		self.assertEqual(engine.evaluate_threshold(s, None, t), models.UNKNOWN)
		self.assertEqual(engine.evaluate_threshold(s, 2.5, None), models.UNKNOWN)
		self.assertEqual(engine.evaluate_threshold(s, 2.5, models.Threshold("current_ratio")), models.UNKNOWN)

	def test_neutral_direction_never_judges(self):
		s = spec("dpo")
		t = models.Threshold("dpo", green_min=30.0, amber_min=60.0, red_max=90.0)
		self.assertEqual(engine.evaluate_threshold(s, 30.0, t), models.UNKNOWN)
		self.assertEqual(engine.evaluate_threshold(s, 300.0, t), models.UNKNOWN)

	def test_only_amber_configured_falls_through_to_red(self):
		s = spec("current_ratio")
		t = models.Threshold("current_ratio", amber_min=1.2)
		self.assertEqual(engine.evaluate_threshold(s, 1.5, t), models.AMBER)
		self.assertEqual(engine.evaluate_threshold(s, 0.9, t), models.RED)

	def test_shipped_defaults_grade_the_reference_inputs(self):
		graded = {}
		for s in registry.ALL_SPECS:
			value = engine.compute_metric(s, INPUTS)
			graded[s.key] = engine.evaluate_threshold(s, value.value, s.default_threshold)
		self.assertEqual(graded["current_ratio"], models.GREEN)  # 2.0 >= 2
		self.assertEqual(graded["quick_ratio"], models.AMBER)  # 0.8 >= 0.8
		self.assertEqual(graded["dso"], models.RED)  # 73 days
		self.assertEqual(graded["otif_pct"], models.GREEN)  # 95%
		self.assertEqual(graded["ar_overdue_pct"], models.RED)  # 25%
		self.assertEqual(graded["dpo"], models.UNKNOWN)  # neutral by design
		self.assertEqual(graded["gross_margin_pct"], models.UNKNOWN)  # no shipped bands


class TestTrend(unittest.TestCase):
	def value(self, key, number):
		return models.MetricValue(key=key, value=number)

	def test_change_and_pct_change(self):
		values = [self.value("revenue", 800000.0), self.value("revenue", 1000000.0)]
		result = engine.trend(values, spec("revenue"))
		self.assertEqual(result["current"], 1000000.0)
		self.assertEqual(result["previous"], 800000.0)
		self.assertEqual(result["change"], 200000.0)
		self.assertEqual(result["pct_change"], 25.0)
		self.assertTrue(result["direction_is_good"])
		self.assertEqual(result["sparkline"], [800000.0, 1000000.0])

	def test_lower_is_better_improves_when_falling(self):
		values = [self.value("dso", 73.0), self.value("dso", 60.0)]
		result = engine.trend(values, spec("dso"))
		self.assertEqual(result["change"], -13.0)
		self.assertAlmostEqual(result["pct_change"], -17.81)
		self.assertTrue(result["direction_is_good"])

	def test_higher_is_better_worsens_when_falling(self):
		values = [self.value("current_ratio", 2.0), self.value("current_ratio", 1.5)]
		result = engine.trend(values, spec("current_ratio"))
		self.assertFalse(result["direction_is_good"])

	def test_zero_baseline_has_no_pct_change(self):
		values = [self.value("revenue", 0.0), self.value("revenue", 5000.0)]
		result = engine.trend(values, spec("revenue"))
		self.assertEqual(result["change"], 5000.0)
		self.assertIsNone(result["pct_change"])

	def test_negative_baseline_uses_magnitude(self):
		values = [self.value("working_capital", -1000.0), self.value("working_capital", -500.0)]
		result = engine.trend(values, spec("working_capital"))
		self.assertEqual(result["change"], 500.0)
		self.assertEqual(result["pct_change"], 50.0)

	def test_none_values_are_guarded(self):
		values = [self.value("dso", None), self.value("dso", 45.0)]
		result = engine.trend(values, spec("dso"))
		self.assertIsNone(result["change"])
		self.assertIsNone(result["pct_change"])
		self.assertFalse(result["direction_is_good"])
		self.assertEqual(result["sparkline"], [None, 45.0])

	def test_single_and_empty_series(self):
		single = engine.trend([self.value("dso", 45.0)], spec("dso"))
		self.assertEqual(single["current"], 45.0)
		self.assertIsNone(single["previous"])
		self.assertIsNone(single["change"])

		empty = engine.trend([], spec("dso"))
		self.assertIsNone(empty["current"])
		self.assertEqual(empty["sparkline"], [])

	def test_neutral_metric_is_never_good_or_bad(self):
		values = [self.value("cogs", 100.0), self.value("cogs", 50.0)]
		self.assertFalse(engine.trend(values, spec("cogs"))["direction_is_good"])

	def test_trend_without_spec_still_computes_math(self):
		values = [self.value("dso", 40.0), self.value("dso", 50.0)]
		result = engine.trend(values)
		self.assertEqual(result["change"], 10.0)
		self.assertEqual(result["pct_change"], 25.0)
		self.assertFalse(result["direction_is_good"])


class TestMetricValueSerialization(unittest.TestCase):
	def test_as_dict_round_trips_dates(self):
		value = engine.compute_metric(spec("dso"), INPUTS, "Wind Power LLC", PERIOD_START, PERIOD_END)
		payload = value.as_dict()
		self.assertEqual(payload["period_start"], "2026-01-01")
		self.assertEqual(payload["period_end"], "2026-01-31")
		self.assertEqual(payload["value"], 73.0)
		self.assertTrue(value.is_available)

	def test_unavailable_value(self):
		value = engine.compute_metric(spec("dso"), {"ar_balance": 1.0, "revenue": 0.0})
		self.assertFalse(value.is_available)


if __name__ == "__main__":
	unittest.main(verbosity=2)
