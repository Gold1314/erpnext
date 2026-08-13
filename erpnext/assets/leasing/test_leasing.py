# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure lease accounting engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/assets/leasing/test_leasing.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.assets.leasing.models import ...``
working without a site (same bootstrap as
erpnext/accounts/forecasting/test_engine.py).
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
	from erpnext.assets.leasing import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.assets.leasing.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.assets", "erpnext.assets.leasing"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.assets.leasing.models", _HERE / "models.py")
	engine = _load_module("erpnext.assets.leasing.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def level_terms(
	start="2026-01-01",
	months=36,
	amount=1000.0,
	rate=6.0,
	timing=models.TIMING_END,
	frequency="Monthly",
):
	commencement = D(start)
	end = engine.add_months(commencement, months) - engine.ONE_DAY
	payments = engine.generate_payment_rows(commencement, end, frequency, amount, timing)
	return models.LeaseTerms(
		commencement_date=commencement,
		end_date=end,
		payments=payments,
		annual_discount_rate_pct=rate,
		payment_timing=timing,
	)


def closed_form_ordinary_annuity_pv(payment, monthly_rate, periods):
	return payment * (1 - (1 + monthly_rate) ** -periods) / monthly_rate


class TestDateHelpers(unittest.TestCase):
	def test_add_months_clamps_day(self):
		self.assertEqual(engine.add_months(D("2026-01-31"), 1), D("2026-02-28"))
		self.assertEqual(engine.add_months(D("2024-01-31"), 1), D("2024-02-29"))
		self.assertEqual(engine.add_months(D("2026-11-30"), 3), D("2027-02-28"))

	def test_term_months(self):
		self.assertEqual(engine.term_months(D("2026-01-01"), D("2026-12-31")), 12)
		self.assertEqual(engine.term_months(D("2026-01-01"), D("2028-12-31")), 36)
		self.assertEqual(engine.term_months(D("2026-01-15"), D("2027-01-14")), 12)
		self.assertEqual(engine.term_months(D("2026-01-01"), D("2026-02-01")), 2)
		self.assertEqual(engine.term_months(D("2026-01-01"), D("2026-01-20")), 1)


class TestPresentValue(unittest.TestCase):
	def test_matches_closed_form_ordinary_annuity(self):
		terms = level_terms(months=36, amount=1000.0, rate=6.0)
		pv = engine.present_value(terms.payments, 6.0, terms.commencement_date, models.TIMING_END)
		expected = closed_form_ordinary_annuity_pv(1000.0, 0.06 / 12, 36)
		self.assertAlmostEqual(pv, expected, delta=0.01)

	def test_beginning_timing_is_annuity_due(self):
		end_terms = level_terms(months=36, amount=1000.0, rate=6.0, timing=models.TIMING_END)
		begin_terms = level_terms(months=36, amount=1000.0, rate=6.0, timing=models.TIMING_BEGINNING)
		pv_end = engine.present_value(end_terms.payments, 6.0, end_terms.commencement_date, models.TIMING_END)
		pv_begin = engine.present_value(
			begin_terms.payments, 6.0, begin_terms.commencement_date, models.TIMING_BEGINNING
		)
		# annuity-due PV = ordinary-annuity PV x (1 + monthly rate)
		self.assertAlmostEqual(pv_begin, pv_end * (1 + 0.06 / 12), delta=0.01)
		self.assertGreater(pv_begin, pv_end)

	def test_payment_at_commencement_is_undiscounted(self):
		payments = [models.LeasePayment(due_date=D("2026-01-01"), amount=500.0)]
		pv = engine.present_value(payments, 12.0, D("2026-01-01"), models.TIMING_BEGINNING)
		self.assertEqual(pv, 500.0)

	def test_zero_rate_pv_is_sum(self):
		terms = level_terms(months=24, amount=750.0, rate=0.0)
		pv = engine.present_value(terms.payments, 0.0, terms.commencement_date, models.TIMING_END)
		self.assertEqual(pv, 24 * 750.0)

	def test_rejects_payment_before_commencement(self):
		payments = [models.LeasePayment(due_date=D("2025-12-31"), amount=100.0)]
		with self.assertRaises(ValueError):
			engine.present_value(payments, 5.0, D("2026-01-01"), models.TIMING_END)


class TestSchedule(unittest.TestCase):
	def assert_schedule_closes(self, schedule):
		last = schedule.rows[-1]
		self.assertEqual(last.closing_liability, 0.0)
		self.assertEqual(last.closing_rou, 0.0)
		# principal repaid over the term equals the initial liability exactly
		self.assertEqual(engine.round2(sum(r.principal for r in schedule.rows)), schedule.initial_liability)
		# depreciation over the term equals the initial ROU exactly
		self.assertEqual(
			engine.round2(sum(r.rou_depreciation for r in schedule.rows)), schedule.initial_rou
		)
		# roll-forward is internally consistent row by row
		for row in schedule.rows:
			self.assertEqual(
				engine.round2(row.opening_liability + row.interest - row.payment), row.closing_liability
			)
			self.assertEqual(engine.round2(row.opening_rou - row.rou_depreciation), row.closing_rou)

	def test_36_month_level_schedule_closes_to_zero(self):
		schedule = engine.build_schedule(level_terms(months=36, amount=1000.0, rate=6.0))
		self.assertEqual(len(schedule.rows), 36)
		self.assert_schedule_closes(schedule)
		self.assertEqual(
			engine.round2(schedule.initial_liability + schedule.total_interest),
			schedule.total_payments,
		)

	def test_60_month_beginning_timing_closes_to_zero(self):
		schedule = engine.build_schedule(
			level_terms(months=60, amount=2350.55, rate=8.4, timing=models.TIMING_BEGINNING)
		)
		self.assertEqual(len(schedule.rows), 60)
		self.assert_schedule_closes(schedule)
		# annuity due: the very first payment carries no interest at all
		self.assertEqual(schedule.rows[0].payment, 2350.55)

	def test_quarterly_payments_close_to_zero(self):
		schedule = engine.build_schedule(
			level_terms(months=36, amount=3000.0, rate=7.25, frequency="Quarterly")
		)
		self.assertEqual(len(schedule.rows), 36)
		self.assertEqual(sum(1 for r in schedule.rows if r.payment), 12)
		self.assert_schedule_closes(schedule)

	def test_uneven_payments_close_to_zero(self):
		commencement = D("2026-03-01")
		end = D("2028-02-29")
		payments = []
		for tick in range(1, 25):
			amount = 800.0 if tick <= 12 else 1234.56
			payments.append(
				models.LeasePayment(
					due_date=engine.add_months(commencement, tick) - engine.ONE_DAY, amount=amount
				)
			)
		terms = models.LeaseTerms(
			commencement_date=commencement,
			end_date=end,
			payments=payments,
			annual_discount_rate_pct=9.99,
			payment_timing=models.TIMING_END,
		)
		schedule = engine.build_schedule(terms)
		self.assertEqual(len(schedule.rows), 24)
		self.assert_schedule_closes(schedule)

	def test_zero_rate_schedule_has_no_interest(self):
		schedule = engine.build_schedule(level_terms(months=24, amount=750.0, rate=0.0))
		self.assertEqual(schedule.initial_liability, 24 * 750.0)
		self.assertEqual(schedule.total_interest, 0.0)
		for row in schedule.rows:
			self.assertEqual(row.interest, 0.0)
			self.assertEqual(row.principal, row.payment)
		self.assert_schedule_closes(schedule)

	def test_rounding_residual_absorbed_in_final_row(self):
		# awkward amount/rate chosen to force per-period rounding drift
		schedule = engine.build_schedule(level_terms(months=37, amount=333.33, rate=7.77))
		self.assert_schedule_closes(schedule)
		# the final row's interest differs from the naive accrual only by the
		# accumulated residual, which must be within a few cents
		last = schedule.rows[-1]
		naive_interest = engine.round2(last.opening_liability * 7.77 / 12 / 100)
		self.assertLessEqual(abs(last.interest - naive_interest), 0.05)

	def test_liability_decreases_monotonically(self):
		schedule = engine.build_schedule(level_terms(months=48, amount=1500.0, rate=5.5))
		for row in schedule.rows:
			self.assertLess(row.closing_liability, row.opening_liability)
			self.assertGreaterEqual(row.closing_liability, 0.0)

	def test_shorter_rou_useful_life(self):
		schedule = engine.build_schedule(level_terms(months=36, amount=1000.0, rate=6.0), 24)
		self.assertEqual(schedule.rows[23].closing_rou, 0.0)
		self.assertEqual(schedule.rows[24].rou_depreciation, 0.0)
		self.assertEqual(schedule.rows[-1].closing_rou, 0.0)
		self.assertEqual(
			engine.round2(sum(r.rou_depreciation for r in schedule.rows)), schedule.initial_rou
		)

	def test_rejects_payment_outside_term(self):
		terms = level_terms(months=12, amount=100.0, rate=5.0)
		terms.payments.append(models.LeasePayment(due_date=D("2029-06-30"), amount=100.0))
		with self.assertRaises(ValueError):
			engine.build_schedule(terms)


class TestShortTermClassification(unittest.TestCase):
	def test_twelve_months_is_short_term(self):
		self.assertTrue(engine.classify_short_term(level_terms(months=12, amount=100.0, rate=5.0)))

	def test_thirteen_months_is_not_short_term(self):
		self.assertFalse(engine.classify_short_term(level_terms(months=13, amount=100.0, rate=5.0)))

	def test_custom_threshold(self):
		terms = level_terms(months=18, amount=100.0, rate=5.0)
		self.assertTrue(engine.classify_short_term(terms, threshold_months=18))
		self.assertFalse(engine.classify_short_term(terms, threshold_months=17))


class TestRemeasurement(unittest.TestCase):
	def build_base(self):
		return engine.build_schedule(level_terms(start="2026-01-01", months=36, amount=1000.0, rate=6.0))

	def remaining_terms(self, effective, rate):
		commencement = D(effective)
		end = D("2028-12-31")
		payments = [
			models.LeasePayment(due_date=engine.add_months(commencement, tick) - engine.ONE_DAY, amount=1000.0)
			for tick in range(1, engine.term_months(commencement, end) + 1)
		]
		return models.LeaseTerms(
			commencement_date=commencement,
			end_date=end,
			payments=payments,
			annual_discount_rate_pct=rate,
			payment_timing=models.TIMING_END,
		)

	def test_lower_rate_increases_liability(self):
		schedule = self.build_base()
		result = engine.remeasure(schedule, D("2027-01-01"), self.remaining_terms("2027-01-01", 3.0))
		self.assertGreater(result.rou_adjustment, 0.0)
		self.assertEqual(
			engine.round2(result.carrying_liability + result.rou_adjustment),
			result.new_schedule.initial_liability,
		)
		self.assert_carrying_matches_old_schedule(schedule, result, D("2027-01-01"))

	def test_higher_rate_decreases_liability(self):
		schedule = self.build_base()
		result = engine.remeasure(schedule, D("2027-01-01"), self.remaining_terms("2027-01-01", 12.0))
		self.assertLess(result.rou_adjustment, 0.0)

	def assert_carrying_matches_old_schedule(self, schedule, result, effective):
		expected = schedule.initial_liability
		for row in schedule.rows:
			if row.period_end < effective:
				expected = row.closing_liability
		self.assertEqual(result.carrying_liability, expected)

	def test_new_schedule_closes(self):
		schedule = self.build_base()
		result = engine.remeasure(schedule, D("2027-01-01"), self.remaining_terms("2027-01-01", 3.0))
		self.assertEqual(result.new_schedule.rows[-1].closing_liability, 0.0)
		self.assertEqual(result.new_schedule.rows[-1].closing_rou, 0.0)

	def test_requires_matching_effective_date(self):
		schedule = self.build_base()
		with self.assertRaises(ValueError):
			engine.remeasure(schedule, D("2027-02-01"), self.remaining_terms("2027-01-01", 3.0))


if __name__ == "__main__":
	unittest.main(verbosity=2)
