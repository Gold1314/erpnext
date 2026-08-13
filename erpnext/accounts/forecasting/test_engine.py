# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure cash-flow forecasting engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/forecasting/test_engine.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.accounts.forecasting.models
import ...`` working without a site.
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
	from erpnext.accounts.forecasting import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.accounts.forecasting.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.forecasting"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.forecasting.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.forecasting.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def item(posting_date, amount, source_type=models.RECEIVABLE, **kwargs):
	return models.CashFlowItem(
		posting_date=D(posting_date) if isinstance(posting_date, str) else posting_date,
		amount=amount,
		source_type=source_type,
		**kwargs,
	)


class TestPeriodBucketing(unittest.TestCase):
	def test_daily_periods_one_bucket_per_day(self):
		periods = engine.build_periods(D("2026-08-13"), D("2026-08-15"), models.DAILY)
		self.assertEqual(len(periods), 3)
		self.assertEqual(periods[0].from_date, D("2026-08-13"))
		self.assertEqual(periods[0].to_date, D("2026-08-13"))
		self.assertEqual(periods[2].from_date, D("2026-08-15"))
		self.assertEqual(periods[0].label, "2026-08-13")

	def test_weekly_periods_anchor_on_start_and_clip_last(self):
		periods = engine.build_periods(D("2026-08-13"), D("2026-08-29"), models.WEEKLY)
		self.assertEqual(len(periods), 3)
		self.assertEqual((periods[0].from_date, periods[0].to_date), (D("2026-08-13"), D("2026-08-19")))
		self.assertEqual((periods[1].from_date, periods[1].to_date), (D("2026-08-20"), D("2026-08-26")))
		# last week clipped to the horizon end
		self.assertEqual((periods[2].from_date, periods[2].to_date), (D("2026-08-27"), D("2026-08-29")))

	def test_monthly_periods_align_to_calendar_months(self):
		periods = engine.build_periods(D("2026-08-13"), D("2026-10-10"), models.MONTHLY)
		self.assertEqual(len(periods), 3)
		# first period is partial: start -> end of August
		self.assertEqual((periods[0].from_date, periods[0].to_date), (D("2026-08-13"), D("2026-08-31")))
		self.assertEqual((periods[1].from_date, periods[1].to_date), (D("2026-09-01"), D("2026-09-30")))
		# last period clipped to the horizon end
		self.assertEqual((periods[2].from_date, periods[2].to_date), (D("2026-10-01"), D("2026-10-10")))
		self.assertEqual(periods[0].label, "Aug 2026")

	def test_monthly_handles_year_rollover_and_leap_february(self):
		periods = engine.build_periods(D("2027-12-15"), D("2028-03-05"), models.MONTHLY)
		self.assertEqual(
			[(p.from_date, p.to_date) for p in periods],
			[
				(D("2027-12-15"), D("2027-12-31")),
				(D("2028-01-01"), D("2028-01-31")),
				(D("2028-02-01"), D("2028-02-29")),  # 2028 is a leap year
				(D("2028-03-01"), D("2028-03-05")),
			],
		)

	def test_single_day_horizon(self):
		for periodicity in models.PERIODICITIES:
			periods = engine.build_periods(D("2026-08-13"), D("2026-08-13"), periodicity)
			self.assertEqual(len(periods), 1, periodicity)
			self.assertEqual(periods[0].from_date, periods[0].to_date)

	def test_invalid_inputs_raise(self):
		with self.assertRaises(ValueError):
			engine.build_forecast([], 0, D("2026-08-13"), D("2026-08-12"), models.DAILY)
		with self.assertRaises(ValueError):
			engine.build_forecast([], 0, D("2026-08-13"), D("2026-08-14"), "Quarterly")


class TestOverdueRollIn(unittest.TestCase):
	def test_items_before_start_land_in_first_period(self):
		items = [
			item("2026-07-01", 500.0),  # overdue receivable
			item("2026-08-20", 300.0),
		]
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-26"), models.WEEKLY)
		self.assertEqual(len(result.periods), 2)
		self.assertEqual(result.periods[0].inflows[models.RECEIVABLE], 500.0)
		self.assertEqual(result.periods[1].inflows[models.RECEIVABLE], 300.0)

	def test_items_after_end_are_dropped(self):
		items = [item("2026-12-31", 999.0)]
		result = engine.build_forecast(items, 100.0, D("2026-08-13"), D("2026-08-26"), models.WEEKLY)
		self.assertEqual(result.periods[0].total_inflow, 0.0)
		self.assertEqual(result.closing_balance, 100.0)


class TestScenario(unittest.TestCase):
	def test_receivable_delay_shifts_across_period_boundary(self):
		items = [item("2026-08-19", 1000.0)]  # last day of week 1
		scenario = models.ForecastScenario(receivable_delay_days=3)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-26"), models.WEEKLY, scenario)
		self.assertEqual(result.periods[0].total_inflow, 0.0)
		self.assertEqual(result.periods[1].inflows[models.RECEIVABLE], 1000.0)

	def test_payable_delay_only_moves_payables(self):
		items = [
			item("2026-08-19", -400.0, models.PAYABLE),
			item("2026-08-19", 1000.0, models.RECEIVABLE),
		]
		scenario = models.ForecastScenario(payable_delay_days=7)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-26"), models.WEEKLY, scenario)
		self.assertEqual(result.periods[0].inflows[models.RECEIVABLE], 1000.0)
		self.assertNotIn(models.PAYABLE, result.periods[0].outflows)
		self.assertEqual(result.periods[1].outflows[models.PAYABLE], 400.0)

	def test_delay_does_not_move_pipeline_or_subscription_items(self):
		items = [
			item("2026-08-14", 100.0, models.SALES_ORDER),
			item("2026-08-14", 50.0, models.SUBSCRIPTION),
		]
		scenario = models.ForecastScenario(receivable_delay_days=30)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-26"), models.WEEKLY, scenario)
		self.assertEqual(result.periods[0].inflows[models.SALES_ORDER], 100.0)
		self.assertEqual(result.periods[0].inflows[models.SUBSCRIPTION], 50.0)

	def test_haircut_scales_pipeline_sources_only(self):
		items = [
			item("2026-08-14", 1000.0, models.SALES_ORDER),
			item("2026-08-14", -500.0, models.PURCHASE_ORDER),
			item("2026-08-14", 200.0, models.RECEIVABLE),
		]
		scenario = models.ForecastScenario(confidence_haircut_pct=20)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-19"), models.WEEKLY, scenario)
		period = result.periods[0]
		self.assertAlmostEqual(period.inflows[models.SALES_ORDER], 800.0)
		self.assertAlmostEqual(period.outflows[models.PURCHASE_ORDER], 400.0)
		self.assertAlmostEqual(period.inflows[models.RECEIVABLE], 200.0)

	def test_haircut_over_100_pct_floors_at_zero(self):
		items = [item("2026-08-14", 1000.0, models.SALES_ORDER)]
		scenario = models.ForecastScenario(confidence_haircut_pct=150)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-19"), models.WEEKLY, scenario)
		self.assertAlmostEqual(result.periods[0].inflows.get(models.SALES_ORDER, 0.0), 0.0)

	def test_include_flags_drop_pipeline_items(self):
		items = [
			item("2026-08-14", 1000.0, models.SALES_ORDER),
			item("2026-08-14", -500.0, models.PURCHASE_ORDER),
		]
		scenario = models.ForecastScenario(include_sales_orders=False, include_purchase_orders=False)
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-19"), models.WEEKLY, scenario)
		self.assertEqual(result.periods[0].total_inflow, 0.0)
		self.assertEqual(result.periods[0].total_outflow, 0.0)

	def test_apply_scenario_does_not_mutate_input(self):
		original = item("2026-08-14", 1000.0, models.SALES_ORDER)
		engine.apply_scenario([original], models.ForecastScenario(confidence_haircut_pct=50))
		self.assertEqual(original.amount, 1000.0)
		self.assertEqual(original.posting_date, D("2026-08-14"))


class TestRunningBalance(unittest.TestCase):
	def test_closing_balance_runs_across_periods(self):
		items = [
			item("2026-08-14", 1000.0, models.RECEIVABLE),
			item("2026-08-15", -300.0, models.PAYABLE),
			item("2026-08-21", -900.0, models.PAYABLE),
			item("2026-08-28", 400.0, models.RECEIVABLE),
		]
		result = engine.build_forecast(items, 250.0, D("2026-08-13"), D("2026-09-02"), models.WEEKLY)
		self.assertEqual(len(result.periods), 3)

		p1, p2, p3 = result.periods
		self.assertEqual((p1.total_inflow, p1.total_outflow, p1.net), (1000.0, 300.0, 700.0))
		self.assertEqual((p1.opening_balance, p1.closing_balance), (250.0, 950.0))
		self.assertEqual((p2.opening_balance, p2.closing_balance), (950.0, 50.0))
		self.assertEqual((p3.opening_balance, p3.closing_balance), (50.0, 450.0))
		self.assertEqual(result.closing_balance, 450.0)
		self.assertEqual(result.opening_balance, 250.0)

	def test_per_source_totals_split_inflow_and_outflow(self):
		items = [
			item("2026-08-13", 100.0, models.SUBSCRIPTION),
			item("2026-08-13", -40.0, models.SUBSCRIPTION),
		]
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-13"), models.DAILY)
		period = result.periods[0]
		self.assertEqual(period.inflows[models.SUBSCRIPTION], 100.0)
		self.assertEqual(period.outflows[models.SUBSCRIPTION], 40.0)
		self.assertEqual(period.net, 60.0)

	def test_empty_items(self):
		result = engine.build_forecast([], 5000.0, D("2026-08-13"), D("2026-08-31"), models.MONTHLY)
		self.assertEqual(len(result.periods), 1)
		self.assertEqual(result.closing_balance, 5000.0)

	def test_source_type_ordering_helpers(self):
		items = [
			item("2026-08-13", 100.0, models.SUBSCRIPTION),
			item("2026-08-13", 100.0, models.RECEIVABLE),
			item("2026-08-13", -40.0, models.PAYABLE),
		]
		result = engine.build_forecast(items, 0.0, D("2026-08-13"), D("2026-08-13"), models.DAILY)
		self.assertEqual(result.inflow_source_types(), [models.RECEIVABLE, models.SUBSCRIPTION])
		self.assertEqual(result.outflow_source_types(), [models.PAYABLE])


if __name__ == "__main__":
	unittest.main(verbosity=2)
