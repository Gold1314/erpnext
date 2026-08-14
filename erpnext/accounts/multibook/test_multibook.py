# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure multi-GAAP adjustment engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/multibook/test_multibook.py

- the modules are loaded directly from their file paths under their canonical
names (same bootstrap as ``erpnext/accounts/forecasting/test_engine.py``).
"""

from __future__ import annotations

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
	from erpnext.accounts.multibook import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.accounts.multibook.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.multibook"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.multibook.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.multibook.engine", _HERE / "engine.py")


def reclassify(name="R1", source="Rent Expense", target="ROU Depreciation", pct=100.0):
	return models.PolicyRule(
		name=name, rule_type=models.RECLASSIFY, source_account=source, target_account=target, percentage=pct
	)


def exclude(name="X1", source="Provision Expense", target="GAAP Adjustment", pct=100.0):
	return models.PolicyRule(
		name=name, rule_type=models.EXCLUDE, source_account=source, target_account=target, percentage=pct
	)


def manual(name="M1", amount=100.0, dr="IFRS Asset", cr="GAAP Adjustment"):
	return models.PolicyRule(
		name=name,
		rule_type=models.MANUAL_AMOUNT,
		manual_amount=amount,
		manual_debit_account=dr,
		manual_credit_account=cr,
	)


def by_account(lines):
	return {line.account: line for line in lines}


def assert_balanced(test, lines):
	test.assertAlmostEqual(
		sum(line.debit for line in lines), sum(line.credit for line in lines), places=2
	)


class TestReclassify(unittest.TestCase):
	def test_net_debit_movement(self):
		# Expense account with net debit 1000 -> credit source, debit target
		lines = engine.build_adjustment_lines([reclassify()], {"Rent Expense": 1000.0})
		self.assertEqual(len(lines), 2)
		got = by_account(lines)
		self.assertEqual((got["Rent Expense"].debit, got["Rent Expense"].credit), (0.0, 1000.0))
		self.assertEqual((got["ROU Depreciation"].debit, got["ROU Depreciation"].credit), (1000.0, 0.0))
		self.assertEqual(got["Rent Expense"].source_rules, ("R1",))
		assert_balanced(self, lines)

	def test_net_credit_movement(self):
		# Income account with net credit 500 (movement -500) -> mirrored
		lines = engine.build_adjustment_lines(
			[reclassify(source="Sales", target="Deferred Revenue")], {"Sales": -500.0}
		)
		got = by_account(lines)
		self.assertEqual((got["Sales"].debit, got["Sales"].credit), (500.0, 0.0))
		self.assertEqual(
			(got["Deferred Revenue"].debit, got["Deferred Revenue"].credit), (0.0, 500.0)
		)
		assert_balanced(self, lines)

	def test_percentage_split(self):
		lines = engine.build_adjustment_lines([reclassify(pct=40.0)], {"Rent Expense": 1000.0})
		got = by_account(lines)
		self.assertEqual(got["Rent Expense"].credit, 400.0)
		self.assertEqual(got["ROU Depreciation"].debit, 400.0)

	def test_percentage_rounds_half_up_at_line_level(self):
		# 100 x 12.345% = 12.345 -> 12.35 half-up (Decimal(str(...)) keeps
		# binary-float noise from flipping the rounding)
		lines = engine.build_adjustment_lines([reclassify(pct=12.345)], {"Rent Expense": 100.0})
		got = by_account(lines)
		self.assertEqual(got["Rent Expense"].credit, 12.35)
		self.assertEqual(got["ROU Depreciation"].debit, 12.35)
		assert_balanced(self, lines)

	def test_zero_movement_produces_no_lines(self):
		self.assertEqual(engine.build_adjustment_lines([reclassify()], {"Rent Expense": 0.0}), [])
		# absent from the movements map == zero movement
		self.assertEqual(engine.build_adjustment_lines([reclassify()], {}), [])


class TestExclude(unittest.TestCase):
	def test_exclude_net_debit(self):
		# Negate 100% of a 200 net-debit provision in this book
		lines = engine.build_adjustment_lines([exclude()], {"Provision Expense": 200.0})
		got = by_account(lines)
		self.assertEqual((got["Provision Expense"].debit, got["Provision Expense"].credit), (0.0, 200.0))
		self.assertEqual((got["GAAP Adjustment"].debit, got["GAAP Adjustment"].credit), (200.0, 0.0))
		assert_balanced(self, lines)

	def test_exclude_net_credit_with_percentage(self):
		lines = engine.build_adjustment_lines([exclude(pct=50.0)], {"Provision Expense": -300.0})
		got = by_account(lines)
		self.assertEqual((got["Provision Expense"].debit, got["Provision Expense"].credit), (150.0, 0.0))
		self.assertEqual((got["GAAP Adjustment"].debit, got["GAAP Adjustment"].credit), (0.0, 150.0))


class TestManualAmount(unittest.TestCase):
	def test_fixed_lines(self):
		lines = engine.build_adjustment_lines([manual(amount=123.456)], {})
		got = by_account(lines)
		self.assertEqual(got["IFRS Asset"].debit, 123.46)  # half-up at line level
		self.assertEqual(got["GAAP Adjustment"].credit, 123.46)
		assert_balanced(self, lines)


class TestConsolidation(unittest.TestCase):
	def test_multi_rule_same_target_sums(self):
		rules = [
			reclassify(name="R1", source="Rent A", target="ROU Depreciation"),
			reclassify(name="R2", source="Rent B", target="ROU Depreciation"),
		]
		lines = engine.build_adjustment_lines(rules, {"Rent A": 100.0, "Rent B": 50.0})
		got = by_account(lines)
		self.assertEqual(len(lines), 3)  # two credits + ONE consolidated debit
		self.assertEqual(got["ROU Depreciation"].debit, 150.0)
		self.assertEqual(got["ROU Depreciation"].source_rules, ("R1", "R2"))
		assert_balanced(self, lines)

	def test_netting_drops_zeroed_account(self):
		# R1 debits T by 100, R2 (source T) credits T by 100 -> T nets to zero
		rules = [
			reclassify(name="R1", source="S", target="T"),
			reclassify(name="R2", source="T", target="U"),
		]
		lines = engine.build_adjustment_lines(rules, {"S": 100.0, "T": 100.0})
		got = by_account(lines)
		self.assertNotIn("T", got)
		self.assertEqual(got["S"].credit, 100.0)
		self.assertEqual(got["U"].debit, 100.0)
		assert_balanced(self, lines)

	def test_mixed_rule_types_consolidate(self):
		rules = [exclude(name="X1", target="GAAP Adjustment"), manual(name="M1", amount=40.0, cr="GAAP Adjustment")]
		lines = engine.build_adjustment_lines(rules, {"Provision Expense": 100.0})
		got = by_account(lines)
		# GAAP Adjustment: debit 100 (exclude offset) - credit 40 (manual) = net debit 60
		self.assertEqual((got["GAAP Adjustment"].debit, got["GAAP Adjustment"].credit), (60.0, 0.0))
		self.assertEqual(got["GAAP Adjustment"].source_rules, ("X1", "M1"))
		assert_balanced(self, lines)


class TestBalanceAssertion(unittest.TestCase):
	def test_cent_residual_pushed_to_largest_debit_line(self):
		lines = [
			models.AdjustmentLine(account="A", debit=100.01),
			models.AdjustmentLine(account="B", credit=60.0),
			models.AdjustmentLine(account="C", credit=40.0),
		]
		balanced = engine.balance_lines(lines)
		got = by_account(balanced)
		self.assertEqual(got["A"].debit, 100.0)  # largest line absorbed the +0.01
		assert_balanced(self, balanced)

	def test_cent_residual_pushed_to_largest_credit_line(self):
		lines = [
			models.AdjustmentLine(account="A", debit=50.0),
			models.AdjustmentLine(account="B", credit=50.01),
		]
		balanced = engine.balance_lines(lines)
		self.assertEqual(by_account(balanced)["B"].credit, 50.0)
		assert_balanced(self, balanced)

	def test_unbalanced_beyond_tolerance_raises(self):
		lines = [
			models.AdjustmentLine(account="A", debit=105.0),
			models.AdjustmentLine(account="B", credit=100.0),
		]
		with self.assertRaises(ValueError):
			engine.balance_lines(lines)

	def test_engine_output_always_balances(self):
		# awkward percentages over awkward movements still balance to 2dp
		rules = [
			reclassify(name="R1", source="A1", target="B1", pct=33.333),
			reclassify(name="R2", source="A2", target="B1", pct=66.667),
			exclude(name="X1", source="A3", target="B1", pct=12.5),
			manual(name="M1", amount=99.995, dr="B1", cr="C1"),
		]
		movements = {"A1": 1000.005, "A2": -777.77, "A3": 0.01}
		lines = engine.build_adjustment_lines(rules, movements)
		assert_balanced(self, lines)
		for line in lines:
			self.assertEqual(round(line.debit, 2), line.debit)
			self.assertEqual(round(line.credit, 2), line.credit)
			self.assertTrue(line.debit or line.credit)
			self.assertFalse(line.debit and line.credit)


class TestRuleValidation(unittest.TestCase):
	def test_source_equals_target_raises(self):
		with self.assertRaises(ValueError):
			engine.build_adjustment_lines([reclassify(source="X", target="X")], {"X": 10.0})

	def test_percentage_out_of_range_raises(self):
		for pct in (0, -5, 150):
			with self.assertRaises(ValueError):
				engine.build_adjustment_lines([reclassify(pct=pct)], {"Rent Expense": 10.0})

	def test_manual_missing_accounts_raises(self):
		rule = models.PolicyRule(name="M", rule_type=models.MANUAL_AMOUNT, manual_amount=10.0)
		with self.assertRaises(ValueError):
			engine.build_adjustment_lines([rule], {})

	def test_manual_zero_amount_raises(self):
		with self.assertRaises(ValueError):
			engine.build_adjustment_lines([manual(amount=0.0)], {})

	def test_unknown_rule_type_raises(self):
		rule = models.PolicyRule(name="Z", rule_type="Bogus")
		with self.assertRaises(ValueError):
			engine.build_adjustment_lines([rule], {})


class TestPeriodsOverlap(unittest.TestCase):
	def test_overlap_and_disjoint(self):
		self.assertTrue(engine.periods_overlap("2026-01-01", "2026-01-31", "2026-01-31", "2026-02-28"))
		self.assertTrue(engine.periods_overlap("2026-01-01", "2026-12-31", "2026-06-01", "2026-06-30"))
		self.assertFalse(engine.periods_overlap("2026-01-01", "2026-01-31", "2026-02-01", "2026-02-28"))
		self.assertFalse(engine.periods_overlap("2026-03-01", "2026-03-31", "2026-01-01", "2026-02-28"))


if __name__ == "__main__":
	unittest.main(verbosity=2)
