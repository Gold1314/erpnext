# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure close-sequencing utilities.

``sequencing.py`` has no frappe dependency, but importing it through the
``erpnext`` package would pull in ``erpnext/__init__.py`` (which imports
frappe). So when run as a plain file -

	python erpnext/accounts/closing/test_sequencing.py

- the module is loaded directly from its file path under its canonical name
(bootstrap pattern borrowed from ``erpnext/accounts/forecasting/test_engine.py``).
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
	from erpnext.accounts.closing import sequencing
except Exception:
	# stand-alone run: register stub packages so the module loads without
	# importing erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.closing"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	sequencing = _load_module("erpnext.accounts.closing.sequencing", _HERE / "sequencing.py")


def row(title, depends_on_titles=""):
	return {"title": title, "depends_on_titles": depends_on_titles}


class TestSplitTitles(unittest.TestCase):
	def test_empty_values(self):
		self.assertEqual(sequencing.split_titles(None), [])
		self.assertEqual(sequencing.split_titles(""), [])
		self.assertEqual(sequencing.split_titles("  ,  , "), [])

	def test_comma_separated_with_whitespace(self):
		self.assertEqual(
			sequencing.split_titles(" Reconcile Banks ,Verify Ledger Health,  "),
			["Reconcile Banks", "Verify Ledger Health"],
		)

	def test_already_a_list(self):
		self.assertEqual(sequencing.split_titles(["A", " B "]), ["A", "B"])


class TestResolveDependencies(unittest.TestCase):
	def test_chain_resolution(self):
		rows = [
			row("Reconcile Banks"),
			row("Run Revaluation", "Reconcile Banks"),
			row("Post PCV", "Run Revaluation, Reconcile Banks"),
		]
		dep_map = sequencing.resolve_dependencies(rows)
		self.assertEqual(
			dep_map,
			{
				"Reconcile Banks": [],
				"Run Revaluation": ["Reconcile Banks"],
				"Post PCV": ["Run Revaluation", "Reconcile Banks"],
			},
		)

	def test_duplicate_dependency_entries_are_deduplicated(self):
		dep_map = sequencing.resolve_dependencies([row("A"), row("B", "A, A ,A")])
		self.assertEqual(dep_map["B"], ["A"])

	def test_missing_title_raises(self):
		with self.assertRaisesRegex(ValueError, "needs a title"):
			sequencing.resolve_dependencies([row("")])

	def test_duplicate_title_raises(self):
		with self.assertRaisesRegex(ValueError, "Duplicate task title 'A'"):
			sequencing.resolve_dependencies([row("A"), row("A")])

	def test_unknown_title_raises(self):
		with self.assertRaisesRegex(ValueError, "Task 'B' depends on unknown task 'Nope'"):
			sequencing.resolve_dependencies([row("A"), row("B", "A, Nope")])

	def test_self_dependency_raises(self):
		with self.assertRaisesRegex(ValueError, "Task 'A' cannot depend on itself"):
			sequencing.resolve_dependencies([row("A", "A")])

	def test_direct_cycle_message_contains_path(self):
		rows = [row("A", "B"), row("B", "A")]
		with self.assertRaises(ValueError) as ctx:
			sequencing.resolve_dependencies(rows)
		message = str(ctx.exception)
		self.assertIn("Dependency cycle detected", message)
		self.assertTrue("A -> B -> A" in message or "B -> A -> B" in message, message)

	def test_indirect_cycle_detected(self):
		rows = [row("A", "C"), row("B", "A"), row("C", "B"), row("D")]
		with self.assertRaises(ValueError) as ctx:
			sequencing.resolve_dependencies(rows)
		self.assertIn("Dependency cycle detected", str(ctx.exception))

	def test_acyclic_diamond_is_accepted(self):
		rows = [row("A"), row("B", "A"), row("C", "A"), row("D", "B, C")]
		dep_map = sequencing.resolve_dependencies(rows)
		self.assertEqual(dep_map["D"], ["B", "C"])


class TestTopologicalOrder(unittest.TestCase):
	def assert_topological(self, dep_map, order):
		self.assertEqual(sorted(order), sorted(dep_map))
		for title, deps in dep_map.items():
			for dep in deps:
				self.assertLess(order.index(dep), order.index(title), f"{dep} must precede {title}")

	def test_chain_order(self):
		dep_map = sequencing.resolve_dependencies(
			[row("Post PCV", "Run Revaluation"), row("Run Revaluation", "Reconcile Banks"), row("Reconcile Banks")]
		)
		order = sequencing.topological_order(dep_map)
		self.assertEqual(order, ["Reconcile Banks", "Run Revaluation", "Post PCV"])

	def test_diamond_preserves_row_order_between_peers(self):
		dep_map = sequencing.resolve_dependencies(
			[row("A"), row("B", "A"), row("C", "A"), row("D", "B, C")]
		)
		order = sequencing.topological_order(dep_map)
		self.assert_topological(dep_map, order)
		self.assertEqual(order, ["A", "B", "C", "D"])


class TestComputeDueDate(unittest.TestCase):
	def test_calendar_day_offset(self):
		period_end = datetime.date(2026, 7, 31)
		self.assertEqual(sequencing.compute_due_date(period_end, 0), datetime.date(2026, 7, 31))
		self.assertEqual(sequencing.compute_due_date(period_end, 5), datetime.date(2026, 8, 5))

	def test_none_offset_treated_as_zero(self):
		period_end = datetime.date(2026, 7, 31)
		self.assertEqual(sequencing.compute_due_date(period_end, None), period_end)


class TestUnblockedLogic(unittest.TestCase):
	def test_no_dependencies_is_unblocked(self):
		self.assertTrue(sequencing.is_unblocked({}, []))

	def test_completed_and_skipped_satisfy(self):
		status_map = {"T1": "Completed", "T2": "Skipped"}
		self.assertTrue(sequencing.is_unblocked(status_map, ["T1", "T2"]))
		self.assertEqual(sequencing.blocking_dependencies(status_map, ["T1", "T2"]), [])

	def test_pending_dependency_blocks(self):
		status_map = {"T1": "Completed", "T2": "Pending", "T3": "In Progress"}
		self.assertFalse(sequencing.is_unblocked(status_map, ["T1", "T2", "T3"]))
		self.assertEqual(sequencing.blocking_dependencies(status_map, ["T1", "T2", "T3"]), ["T2", "T3"])

	def test_unknown_dependency_blocks(self):
		self.assertFalse(sequencing.is_unblocked({}, ["Ghost"]))
		self.assertEqual(sequencing.blocking_dependencies({}, ["Ghost"]), ["Ghost"])


if __name__ == "__main__":
	unittest.main(verbosity=2)
