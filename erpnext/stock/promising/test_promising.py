# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure ATP promise engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/stock/promising/test_promising.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.stock.promising.models import ...``
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
	from erpnext.stock.promising import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.stock.promising.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.stock", "erpnext.stock.promising"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.stock.promising.models", _HERE / "models.py")
	engine = _load_module("erpnext.stock.promising.engine", _HERE / "engine.py")


TODAY = datetime.date(2026, 8, 13)
HORIZON_END = TODAY + datetime.timedelta(days=90)


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def supply(date, qty, source_type=models.ON_HAND, reference=None):
	return models.SupplyEvent(
		available_date=D(date) if isinstance(date, str) else date,
		qty=qty,
		source_type=source_type,
		reference=reference,
	)


def demand(date, qty, reference=None):
	return models.DemandEvent(
		required_date=D(date) if isinstance(date, str) else date, qty=qty, reference=reference
	)


def request(qty, requested_date=None, item_code="ITEM-1", warehouse="WH-1"):
	return models.PromiseRequest(
		item_code=item_code,
		warehouse=warehouse,
		qty=qty,
		requested_date=D(requested_date) if isinstance(requested_date, str) else requested_date,
	)


def run_promise(req, supply_events, demand_events=(), horizon_end=HORIZON_END, today=TODAY):
	return engine.promise(req, list(supply_events), list(demand_events), horizon_end, today)


class TestPromise(unittest.TestCase):
	def test_on_hand_only_promises_today(self):
		result = run_promise(request(5), [supply("2026-08-13", 10)])

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, TODAY)
		self.assertEqual(result.shortfall, 0.0)
		self.assertEqual(len(result.allocation), 1)
		self.assertEqual(result.allocation[0].source_type, models.ON_HAND)
		self.assertEqual(result.allocation[0].qty, 5)

	def test_promise_waits_for_po_receipt_date(self):
		result = run_promise(
			request(15),
			[
				supply("2026-08-13", 10),
				supply("2026-09-01", 20, models.PURCHASE_ORDER, "PUR-ORD-0001"),
			],
		)

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, D("2026-09-01"))
		self.assertEqual(
			[(part.source_type, part.qty) for part in result.allocation],
			[(models.ON_HAND, 10), (models.PURCHASE_ORDER, 5)],
		)

	def test_committed_demand_consumes_early_supply_and_pushes_promise_later(self):
		# 10 on hand, 10 arriving 2026-09-01; an existing SO for 10 due
		# 2026-08-20 claims the on-hand stock first, so a new request for 10
		# must wait for the PO.
		result = run_promise(
			request(10),
			[supply("2026-08-13", 10), supply("2026-09-01", 10, models.PURCHASE_ORDER, "PUR-ORD-0001")],
			[demand("2026-08-20", 10, "SAL-ORD-0001")],
		)

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, D("2026-09-01"))
		self.assertEqual(result.allocation[0].source_type, models.PURCHASE_ORDER)

	def test_promise_many_allocates_sequentially_without_double_counting(self):
		key = ("ITEM-1", "WH-1")
		supply_by_key = {key: [supply("2026-08-13", 10), supply("2026-09-01", 10, models.WORK_ORDER, "MFG-WO-1")]}
		demand_by_key = {key: []}

		results = engine.promise_many(
			[request(8), request(8), request(8)], supply_by_key, demand_by_key, HORIZON_END, TODAY
		)

		# line 1 takes 8 on hand; line 2 takes the last 2 on hand + 6 from
		# the WO; line 3 needs 8 but only 4 remain in the pool.
		self.assertEqual(results[0].promised_date, TODAY)
		self.assertEqual(results[1].promised_date, D("2026-09-01"))
		self.assertFalse(results[2].fulfillable)
		self.assertAlmostEqual(results[2].shortfall, 4.0)

	def test_shortfall_qty_when_supply_never_reaches_request(self):
		result = run_promise(
			request(25),
			[supply("2026-08-13", 10), supply("2026-09-01", 5, models.PURCHASE_ORDER, "PUR-ORD-0001")],
		)

		self.assertFalse(result.fulfillable)
		self.assertIsNone(result.promised_date)
		self.assertAlmostEqual(result.shortfall, 10.0)
		self.assertIn("90-day horizon", result.message)

	def test_supply_beyond_horizon_is_ignored(self):
		result = run_promise(
			request(10),
			[
				supply("2026-08-13", 4),
				# arrives one day past the 90-day horizon
				supply(HORIZON_END + datetime.timedelta(days=1), 100, models.PURCHASE_ORDER, "PUR-ORD-0009"),
			],
		)

		self.assertFalse(result.fulfillable)
		self.assertAlmostEqual(result.shortfall, 6.0)

	def test_requested_date_earlier_than_availability_promises_availability(self):
		result = run_promise(
			request(10, requested_date="2026-08-15"),
			[supply("2026-09-01", 10, models.PURCHASE_ORDER, "PUR-ORD-0001")],
		)

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, D("2026-09-01"))
		self.assertIn("2026-08-15", result.message)

	def test_requested_date_later_than_availability_is_honored(self):
		result = run_promise(request(5, requested_date="2026-10-01"), [supply("2026-08-13", 10)])

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, D("2026-10-01"))

	def test_past_dated_supply_is_treated_as_available_today(self):
		# an overdue PO scheduled last week still cannot be promised in the past
		result = run_promise(request(5), [supply("2026-08-01", 10, models.PURCHASE_ORDER, "PUR-ORD-0001")])

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, TODAY)

	def test_zero_and_negative_qty_requests_are_guarded(self):
		for qty in (0, -3):
			result = run_promise(request(qty), [supply("2026-08-13", 10)])
			self.assertFalse(result.fulfillable)
			self.assertIsNone(result.promised_date)
			self.assertEqual(result.shortfall, 0.0)
			self.assertEqual(result.allocation, [])
			self.assertIn("greater than zero", result.message)

	def test_negative_or_zero_supply_and_demand_events_are_ignored(self):
		result = run_promise(
			request(5),
			[supply("2026-08-13", -4), supply("2026-08-13", 0), supply("2026-08-13", 5)],
			[demand("2026-08-14", -2), demand("2026-08-14", 0)],
		)

		self.assertTrue(result.fulfillable)
		self.assertEqual(result.promised_date, TODAY)


class TestNetting(unittest.TestCase):
	def test_demand_consumes_earliest_supply_first(self):
		remaining = engine.net_supply_against_demand(
			[supply("2026-08-13", 10), supply("2026-09-01", 10, models.PURCHASE_ORDER, "PUR-ORD-0001")],
			[demand("2026-08-20", 6)],
			HORIZON_END,
			TODAY,
		)

		self.assertEqual([(event.available_date, event.qty) for event in remaining], [(TODAY, 4), (D("2026-09-01"), 10)])

	def test_demand_beyond_supply_leaves_nothing(self):
		remaining = engine.net_supply_against_demand(
			[supply("2026-08-13", 10)], [demand("2026-08-20", 25)], HORIZON_END, TODAY
		)

		self.assertEqual(remaining, [])

	def test_late_demand_still_claims_early_supply(self):
		# demand due after the horizon still consumes supply: never double-promise
		remaining = engine.net_supply_against_demand(
			[supply("2026-08-13", 10)],
			[demand(HORIZON_END + datetime.timedelta(days=30), 10)],
			HORIZON_END,
			TODAY,
		)

		self.assertEqual(remaining, [])


if __name__ == "__main__":
	unittest.main(verbosity=2)
