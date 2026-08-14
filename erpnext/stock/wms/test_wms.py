# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure WMS-lite engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/stock/wms/test_wms.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.stock.wms.models import ...``
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
	from erpnext.stock.wms import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.stock.wms.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.stock", "erpnext.stock.wms"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.stock.wms.models", _HERE / "models.py")
	engine = _load_module("erpnext.stock.wms.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def loc(code, location_type=models.BIN, pick_sequence=0, capacity_qty=0.0, current_qty=0.0):
	return models.LocationInfo(
		code=code,
		warehouse="WH",
		location_type=location_type,
		pick_sequence=pick_sequence,
		capacity_qty=capacity_qty,
		current_qty=current_qty,
	)


class TestSuggestPutaway(unittest.TestCase):
	def test_fills_capacity_and_overflows_in_sequence_order(self):
		locations = [
			loc("BIN-2", pick_sequence=2, capacity_qty=50, current_qty=10),  # 40 free
			loc("BIN-1", pick_sequence=1, capacity_qty=30, current_qty=0),  # 30 free
		]
		suggestions, remainder = engine.suggest_putaway(locations, 60)

		self.assertEqual(
			[(s.location_code, s.qty) for s in suggestions],
			[("BIN-1", 30.0), ("BIN-2", 30.0)],
		)
		self.assertEqual(remainder, 0.0)

	def test_overflow_remainder_when_all_finite_locations_full(self):
		locations = [
			loc("BIN-1", capacity_qty=20, current_qty=15),  # 5 free
			loc("BIN-2", capacity_qty=10, current_qty=10),  # full -> skipped
		]
		suggestions, remainder = engine.suggest_putaway(locations, 12)

		self.assertEqual([(s.location_code, s.qty) for s in suggestions], [("BIN-1", 5.0)])
		self.assertEqual(remainder, 7.0)

	def test_unlimited_capacity_is_last_resort_and_absorbs_remainder(self):
		locations = [
			loc("OVERFLOW", location_type=models.BULK, pick_sequence=1, capacity_qty=0),
			loc("BIN-1", pick_sequence=9, capacity_qty=10, current_qty=0),
		]
		suggestions, remainder = engine.suggest_putaway(locations, 25)

		# finite bin first despite its worse pick sequence; unlimited catches the rest
		self.assertEqual(
			[(s.location_code, s.qty) for s in suggestions],
			[("BIN-1", 10.0), ("OVERFLOW", 15.0)],
		)
		self.assertEqual(remainder, 0.0)

	def test_type_preference_bin_and_bulk_over_staging(self):
		locations = [
			loc("STAGE-1", location_type=models.STAGING, pick_sequence=1, capacity_qty=100),
			loc("BULK-1", location_type=models.BULK, pick_sequence=1, capacity_qty=100),
			loc("BIN-1", location_type=models.BIN, pick_sequence=9, capacity_qty=100),
		]
		suggestions, _remainder = engine.suggest_putaway(locations, 250)

		# Bin beats Bulk beats Staging even when its pick sequence is higher
		self.assertEqual(
			[s.location_code for s in suggestions],
			["BIN-1", "BULK-1", "STAGE-1"],
		)

	def test_zero_incoming_qty_is_a_no_op(self):
		suggestions, remainder = engine.suggest_putaway([loc("BIN-1", capacity_qty=10)], 0)
		self.assertEqual(suggestions, [])
		self.assertEqual(remainder, 0.0)

	def test_deterministic_tiebreak_by_code(self):
		locations = [
			loc("BIN-B", capacity_qty=100),
			loc("BIN-A", capacity_qty=100),
		]
		suggestions, _ = engine.suggest_putaway(locations, 150)
		self.assertEqual([s.location_code for s in suggestions], ["BIN-A", "BIN-B"])


class TestSuggestPicks(unittest.TestCase):
	def test_pick_path_ordering_by_sequence(self):
		locations = [
			loc("BIN-3", pick_sequence=3, current_qty=10),
			loc("BIN-1", pick_sequence=1, current_qty=4),
			loc("BIN-2", pick_sequence=2, current_qty=6),
		]
		suggestions, shortfall = engine.suggest_picks(locations, 12)

		self.assertEqual(
			[(s.location_code, s.qty, s.pick_sequence) for s in suggestions],
			[("BIN-1", 4.0, 1), ("BIN-2", 6.0, 2), ("BIN-3", 2.0, 3)],
		)
		self.assertEqual(shortfall, 0.0)

	def test_largest_qty_tiebreak_within_same_sequence(self):
		locations = [
			loc("BIN-SMALL", pick_sequence=1, current_qty=2),
			loc("BIN-BIG", pick_sequence=1, current_qty=8),
		]
		suggestions, shortfall = engine.suggest_picks(locations, 8)

		self.assertEqual([(s.location_code, s.qty) for s in suggestions], [("BIN-BIG", 8.0)])
		self.assertEqual(shortfall, 0.0)

	def test_shortfall_reported_when_stock_insufficient(self):
		locations = [loc("BIN-1", pick_sequence=1, current_qty=3)]
		suggestions, shortfall = engine.suggest_picks(locations, 10)

		self.assertEqual([(s.location_code, s.qty) for s in suggestions], [("BIN-1", 3.0)])
		self.assertEqual(shortfall, 7.0)

	def test_empty_locations_are_skipped(self):
		locations = [
			loc("BIN-EMPTY", pick_sequence=1, current_qty=0),
			loc("BIN-1", pick_sequence=2, current_qty=5),
		]
		suggestions, _ = engine.suggest_picks(locations, 5)
		self.assertEqual([s.location_code for s in suggestions], ["BIN-1"])


class TestClassifyABC(unittest.TestCase):
	def test_classic_80_15_5_value_shares(self):
		classes = engine.classify_abc({"ITEM-A": 80.0, "ITEM-B": 15.0, "ITEM-C": 5.0})
		self.assertEqual(classes, {"ITEM-A": "A", "ITEM-B": "B", "ITEM-C": "C"})

	def test_many_items_split_on_cumulative_share(self):
		# 40+40 = 80% -> A; next 15% -> B; last 5% -> C
		velocity = {"I1": 40.0, "I2": 40.0, "I3": 10.0, "I4": 5.0, "I5": 5.0}
		classes = engine.classify_abc(velocity)
		self.assertEqual(classes["I1"], "A")
		self.assertEqual(classes["I2"], "A")
		self.assertEqual(classes["I3"], "B")
		self.assertEqual(classes["I4"], "B")  # before-share 0.90 < 0.95
		self.assertEqual(classes["I5"], "C")

	def test_single_item_is_class_a(self):
		self.assertEqual(engine.classify_abc({"ONLY": 123.0}), {"ONLY": "A"})

	def test_zero_velocity_everything_is_c(self):
		classes = engine.classify_abc({"I1": 0.0, "I2": 0.0})
		self.assertEqual(classes, {"I1": "C", "I2": "C"})

	def test_zero_velocity_item_among_movers_is_c(self):
		classes = engine.classify_abc({"FAST": 100.0, "DEAD": 0.0})
		self.assertEqual(classes, {"FAST": "A", "DEAD": "C"})

	def test_deterministic_ordering_on_ties(self):
		# equal velocities: ranked by item code, so the split is stable
		first = engine.classify_abc({"B": 50.0, "A": 50.0}, a_pct=0.5)
		second = engine.classify_abc({"A": 50.0, "B": 50.0}, a_pct=0.5)
		self.assertEqual(first, second)
		self.assertEqual(first["A"], "A")
		self.assertEqual(first["B"], "B")

	def test_invalid_boundaries_raise(self):
		with self.assertRaises(ValueError):
			engine.classify_abc({"A": 1.0}, a_pct=0.9, b_pct=0.5)


class TestNextCountDue(unittest.TestCase):
	def test_due_after_frequency_days(self):
		due = engine.next_count_due(D("2026-08-01"), 30, D("2026-08-13"))
		self.assertEqual(due, D("2026-08-31"))

	def test_never_counted_is_due_now(self):
		self.assertEqual(engine.next_count_due(None, 30, D("2026-08-13")), D("2026-08-13"))

	def test_is_count_due_boundaries(self):
		self.assertTrue(engine.is_count_due(D("2026-07-01"), 30, D("2026-08-13")))  # overdue
		self.assertTrue(engine.is_count_due(D("2026-07-14"), 30, D("2026-08-13")))  # due today
		self.assertFalse(engine.is_count_due(D("2026-08-01"), 30, D("2026-08-13")))  # not yet

	def test_non_positive_frequency_raises(self):
		with self.assertRaises(ValueError):
			engine.next_count_due(D("2026-08-01"), 0, D("2026-08-13"))


if __name__ == "__main__":
	unittest.main(verbosity=2)
