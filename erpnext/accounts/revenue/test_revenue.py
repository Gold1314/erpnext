# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure revenue recognition engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/revenue/test_revenue.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.accounts.revenue.models import ...``
working without a site.
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
	from erpnext.accounts.revenue import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.accounts.revenue.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.revenue"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.revenue.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.revenue.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def ob(key, stated, ssp, method=models.OVER_TIME, start=None, end=None, satisfied=None):
	return models.ObligationInput(
		key=key,
		description=f"Obligation {key}",
		stated_amount=stated,
		ssp=ssp,
		satisfaction_method=method,
		start_date=D(start) if start else None,
		end_date=D(end) if end else None,
		satisfied_date=D(satisfied) if satisfied else None,
	)


def plan_total(plan):
	return round(sum(row.amount for row in plan.recognition_rows), 2)


def rows_for(plan, key):
	return [row for row in plan.recognition_rows if row.key == key]


class TestAllocate(unittest.TestCase):
	def test_relative_ssp_allocation(self):
		obligations = [ob("lic", 700, 800, models.POINT_IN_TIME), ob("sup", 300, 200)]
		allocations = engine.allocate(1000, obligations)
		self.assertEqual([a.allocated_amount for a in allocations], [800.0, 200.0])
		self.assertEqual([a.allocation_pct for a in allocations], [80.0, 20.0])

	def test_rounding_residual_goes_to_largest_allocation(self):
		# 100 / 3 -> 33.33 each = 99.99; the 0.01 residual lands on the
		# largest allocation (first on ties) so the sum is exact.
		obligations = [ob(k, 0, 1, models.POINT_IN_TIME) for k in ("a", "b", "c")]
		allocations = engine.allocate(100, obligations)
		amounts = [a.allocated_amount for a in allocations]
		self.assertEqual(sum(amounts), 100.0)
		self.assertEqual(amounts, [33.34, 33.33, 33.33])

	def test_residual_prefers_strictly_largest(self):
		obligations = [ob("a", 0, 1, models.POINT_IN_TIME), ob("b", 0, 2, models.POINT_IN_TIME)]
		allocations = engine.allocate(100.01, obligations)
		by_key = {a.key: a.allocated_amount for a in allocations}
		self.assertEqual(round(sum(by_key.values()), 2), 100.01)
		# raw: a=33.336667->33.34, b=66.673333->66.67, residual 0 here; force one:
		allocations = engine.allocate(0.05, obligations)
		by_key = {a.key: a.allocated_amount for a in allocations}
		self.assertEqual(round(sum(by_key.values()), 2), 0.05)
		self.assertGreaterEqual(by_key["b"], by_key["a"])

	def test_zero_ssp_obligation_gets_zero(self):
		obligations = [ob("paid", 900, 1000, models.POINT_IN_TIME), ob("free", 100, 0)]
		allocations = engine.allocate(1000, obligations)
		by_key = {a.key: a.allocated_amount for a in allocations}
		self.assertEqual(by_key["free"], 0.0)
		self.assertEqual(by_key["paid"], 1000.0)

	def test_all_zero_ssp_falls_back_to_stated_amounts(self):
		obligations = [ob("a", 600, 0, models.POINT_IN_TIME), ob("b", 400, 0)]
		allocations = engine.allocate(1000, obligations)
		by_key = {a.key: a.allocated_amount for a in allocations}
		self.assertEqual(by_key, {"a": 600.0, "b": 400.0})

	def test_no_basis_at_all_raises_for_nonzero_price(self):
		obligations = [ob("a", 0, 0, models.POINT_IN_TIME)]
		with self.assertRaises(ValueError):
			engine.allocate(100, obligations)
		# zero price is fine: all-zero allocations
		allocations = engine.allocate(0, obligations)
		self.assertEqual(allocations[0].allocated_amount, 0.0)

	def test_single_obligation_passthrough(self):
		obligations = [ob("only", 1234.56, 999)]
		allocations = engine.allocate(1234.56, obligations)
		self.assertEqual(len(allocations), 1)
		self.assertEqual(allocations[0].allocated_amount, 1234.56)
		self.assertEqual(allocations[0].allocation_pct, 100.0)

	def test_empty_obligations(self):
		self.assertEqual(engine.allocate(100, []), [])


class TestBuildRecognitionPlan(unittest.TestCase):
	def test_point_in_time_single_row(self):
		obligations = [ob("dn", 500, 500, models.POINT_IN_TIME, satisfied="2026-02-15")]
		plan = engine.build_recognition_plan(500, obligations)
		self.assertEqual(len(plan.recognition_rows), 1)
		row = plan.recognition_rows[0]
		self.assertEqual((row.period_start, row.period_end), (D("2026-02-15"), D("2026-02-15")))
		self.assertEqual(row.amount, 500.0)
		self.assertEqual(plan.event_pending_keys, [])

	def test_point_in_time_unsatisfied_is_flagged_on_event(self):
		obligations = [ob("dn", 500, 500, models.POINT_IN_TIME)]
		plan = engine.build_recognition_plan(500, obligations)
		self.assertEqual(len(plan.recognition_rows), 1)
		row = plan.recognition_rows[0]
		self.assertIsNone(row.period_start)
		self.assertIsNone(row.period_end)
		self.assertEqual(row.amount, 500.0)
		self.assertEqual(plan.event_pending_keys, ["dn"])

	def test_over_time_full_months_straight_line(self):
		obligations = [ob("sub", 1200, 1200, start="2026-01-01", end="2026-12-31")]
		plan = engine.build_recognition_plan(1200, obligations)
		self.assertEqual(len(plan.recognition_rows), 12)
		self.assertTrue(all(row.amount == 100.0 for row in plan.recognition_rows))
		self.assertEqual(plan.recognition_rows[0].period_start, D("2026-01-01"))
		self.assertEqual(plan.recognition_rows[0].period_end, D("2026-01-31"))
		self.assertEqual(plan.recognition_rows[-1].period_start, D("2026-12-01"))
		self.assertEqual(plan.recognition_rows[-1].period_end, D("2026-12-31"))

	def test_over_time_partial_first_and_last_month_proration(self):
		# Jan 15 - Mar 15: 17/31 of January, all of February, 15/31 of March
		obligations = [ob("svc", 1000, 1000, start="2026-01-15", end="2026-03-15")]
		plan = engine.build_recognition_plan(1000, obligations)
		rows = plan.recognition_rows
		self.assertEqual(len(rows), 3)
		self.assertEqual((rows[0].period_start, rows[0].period_end), (D("2026-01-15"), D("2026-01-31")))
		self.assertEqual((rows[1].period_start, rows[1].period_end), (D("2026-02-01"), D("2026-02-28")))
		self.assertEqual((rows[2].period_start, rows[2].period_end), (D("2026-03-01"), D("2026-03-15")))

		fractions = [17 / 31, 1.0, 15 / 31]
		total = sum(fractions)
		self.assertEqual(rows[0].amount, round(1000 * fractions[0] / total, 2))
		self.assertEqual(rows[1].amount, round(1000 * fractions[1] / total, 2))
		# partial months recognize less than the full month
		self.assertLess(rows[0].amount, rows[1].amount)
		self.assertLess(rows[2].amount, rows[1].amount)
		# residual lands in the final period so the obligation ties out
		self.assertEqual(plan_total(plan), 1000.0)

	def test_plan_totals_equal_allocations_and_price(self):
		obligations = [
			ob("lic", 500, 700, models.POINT_IN_TIME, satisfied="2026-01-10"),
			ob("impl", 200, 100, models.POINT_IN_TIME),
			ob("sup", 300, 200, start="2026-01-10", end="2026-07-09"),
		]
		plan = engine.build_recognition_plan(1000, obligations)
		self.assertEqual(plan_total(plan), 1000.0)
		allocated = {a.key: a.allocated_amount for a in plan.allocations}
		for key, amount in allocated.items():
			self.assertEqual(round(sum(r.amount for r in rows_for(plan, key)), 2), amount)
		self.assertEqual(plan.event_pending_keys, ["impl"])
		self.assertEqual(plan.total_transaction_price, 1000.0)

	def test_over_time_requires_dates(self):
		with self.assertRaises(ValueError):
			engine.build_recognition_plan(100, [ob("x", 100, 100)])
		with self.assertRaises(ValueError):
			engine.build_recognition_plan(100, [ob("x", 100, 100, start="2026-02-01", end="2026-01-01")])

	def test_unsupported_periodicity(self):
		with self.assertRaises(ValueError):
			engine.build_recognition_plan(
				100,
				[ob("x", 100, 100, start="2026-01-01", end="2026-02-28")],
				periodicity="Quarterly",
			)


class TestModification(unittest.TestCase):
	def _new_obligations(self):
		return [
			ob("a", 0, 900, start="2026-04-01", end="2026-09-30"),
			ob("b", 0, 300, models.POINT_IN_TIME),
		]

	def test_prospective_reallocates_only_unrecognized_remainder(self):
		recognized = {"a": 300.0, "b": 100.0}
		result = engine.modification(
			original_plan=models.ContractPlan(total_transaction_price=1000.0),
			recognized_to_date_by_key=recognized,
			new_transaction_price=1600.0,
			new_obligations=self._new_obligations(),
			method=models.PROSPECTIVE,
		)
		self.assertEqual(result.catch_up_by_key, {})
		# remainder 1600 - 400 = 1200, split 900:300 by SSP
		self.assertEqual(result.plan.total_transaction_price, 1200.0)
		self.assertEqual(plan_total(result.plan), 1200.0)
		allocated = {a.key: a.allocated_amount for a in result.plan.allocations}
		self.assertEqual(allocated, {"a": 900.0, "b": 300.0})
		# remaining periods only: plan starts at the modification window
		self.assertEqual(rows_for(result.plan, "a")[0].period_start, D("2026-04-01"))

	def test_prospective_rejects_over_recognized_contract(self):
		with self.assertRaises(ValueError):
			engine.modification(
				original_plan=models.ContractPlan(total_transaction_price=1000.0),
				recognized_to_date_by_key={"a": 900.0},
				new_transaction_price=800.0,
				new_obligations=self._new_obligations(),
				method=models.PROSPECTIVE,
			)

	def test_cumulative_catch_up_positive_and_negative(self):
		new_obligations = [
			# 1200 over 2026 -> 100/month, cumulative 300 by Mar 31
			ob("a", 1200, 1200, start="2026-01-01", end="2026-12-31"),
			# satisfied before as_of -> full 600 cumulative
			ob("b", 600, 600, models.POINT_IN_TIME, satisfied="2026-02-15"),
		]
		recognized = {"a": 500.0, "b": 0.0}
		result = engine.modification(
			original_plan=models.ContractPlan(total_transaction_price=1800.0),
			recognized_to_date_by_key=recognized,
			new_transaction_price=1800.0,
			new_obligations=new_obligations,
			method=models.CUMULATIVE_CATCH_UP,
			as_of=D("2026-03-31"),
		)
		# a: new cumulative 300 - recognized 500 = -200 (clawback)
		# b: new cumulative 600 - recognized 0 = +600
		self.assertEqual(result.catch_up_by_key, {"a": -200.0, "b": 600.0})
		self.assertEqual(plan_total(result.plan), 1800.0)

	def test_cumulative_catch_up_requires_as_of(self):
		with self.assertRaises(ValueError):
			engine.modification(
				original_plan=models.ContractPlan(total_transaction_price=1000.0),
				recognized_to_date_by_key={},
				new_transaction_price=1000.0,
				new_obligations=self._new_obligations(),
				method=models.CUMULATIVE_CATCH_UP,
			)

	def test_unknown_method(self):
		with self.assertRaises(ValueError):
			engine.modification(
				original_plan=models.ContractPlan(total_transaction_price=1000.0),
				recognized_to_date_by_key={},
				new_transaction_price=1000.0,
				new_obligations=self._new_obligations(),
				method="Retrospective",
			)


if __name__ == "__main__":
	unittest.main(verbosity=2)
