# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure sourcing / qualification engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/buying/sourcing/test_sourcing.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the engine's ``from erpnext.buying.sourcing.models import ...``
working without a site (same bootstrap as erpnext/stock/wms/test_wms.py).
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
import types
import typing
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
	from erpnext.buying.sourcing import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.buying.sourcing.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.buying", "erpnext.buying.sourcing"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.buying.sourcing.models", _HERE / "models.py")
	engine = _load_module("erpnext.buying.sourcing.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def answer(key, score, max_score=5.0, weight=1.0, is_knockout=False, passed=True):
	return models.QuestionnaireAnswer(
		question_key=key,
		weight=weight,
		score=score,
		max_score=max_score,
		is_knockout=is_knockout,
		passed=passed,
	)


def doc(doc_type, provided=True, expiry=None, mandatory=True, requires_expiry=True):
	return models.DocumentRequirement(
		doc_type=doc_type,
		is_mandatory=mandatory,
		requires_expiry=requires_expiry,
		provided=provided,
		expiry_date=D(expiry) if expiry else None,
	)


def bid(supplier, item_code, qty=1.0, unit_price=0.0, lead_time_days=0.0):
	return models.BidLine(
		supplier=supplier,
		item_code=item_code,
		qty=qty,
		unit_price=unit_price,
		lead_time_days=lead_time_days,
	)


class TestQuestionnaireScoring(unittest.TestCase):
	def test_weighted_math(self):
		# (4x2 + 3x1) / (5x2 + 5x1) = 11/15 = 73.33%
		result = engine.score_questionnaire(
			[answer("financials", 4, weight=2), answer("capacity", 3, weight=1)],
			pass_threshold_pct=70,
		)
		self.assertAlmostEqual(result.total_score, 11.0)
		self.assertAlmostEqual(result.max_score, 15.0)
		self.assertAlmostEqual(result.percent, 73.33, places=2)
		self.assertTrue(result.passed)
		self.assertEqual(result.risk_tier, models.RISK_MEDIUM)
		self.assertEqual(result.knockout_failures, [])

	def test_unweighted_rows_and_clamping(self):
		# weight 0 drops out of the ratio; an over-max score is clamped
		result = engine.score_questionnaire(
			[answer("a", 9, max_score=5), answer("ignored", 5, weight=0)],
			pass_threshold_pct=50,
		)
		self.assertAlmostEqual(result.percent, 100.0)
		self.assertAlmostEqual(result.max_score, 5.0)

	def test_no_scorable_rows(self):
		result = engine.score_questionnaire([], pass_threshold_pct=0)
		self.assertEqual(result.percent, 0.0)
		self.assertTrue(result.passed)  # threshold 0
		self.assertEqual(result.risk_tier, models.RISK_HIGH)

	def test_threshold_boundary_passes(self):
		result = engine.score_questionnaire([answer("a", 3.5, max_score=5)], pass_threshold_pct=70)
		self.assertAlmostEqual(result.percent, 70.0)
		self.assertTrue(result.passed)

	def test_knockout_overrides_high_score(self):
		result = engine.score_questionnaire(
			[
				answer("quality", 5, weight=3),
				answer("insurance", 5, weight=1, is_knockout=True, passed=False),
			],
			pass_threshold_pct=70,
		)
		self.assertAlmostEqual(result.percent, 100.0)
		self.assertFalse(result.passed)
		self.assertEqual(result.knockout_failures, ["insurance"])
		# a failed compliance gate is never "Low risk"
		self.assertEqual(result.risk_tier, models.RISK_HIGH)

	def test_unscored_knockout_still_fails(self):
		result = engine.score_questionnaire(
			[answer("a", 5), answer("gate", 0, max_score=0, weight=0, is_knockout=True, passed=False)],
			pass_threshold_pct=70,
		)
		self.assertFalse(result.passed)
		self.assertEqual(result.knockout_failures, ["gate"])

	def test_risk_band_boundaries(self):
		# 85 -> Low (inclusive), 84.x -> Medium, 70 -> Medium, 69.x -> High
		cases = [
			(4.25, models.RISK_LOW),  # 85.0
			(4.24, models.RISK_MEDIUM),  # 84.8
			(3.5, models.RISK_MEDIUM),  # 70.0
			(3.49, models.RISK_HIGH),  # 69.8
		]
		for score, tier in cases:
			result = engine.score_questionnaire([answer("a", score, max_score=5)])
			self.assertEqual(result.risk_tier, tier, f"score {score} -> {result.percent}%")

	def test_custom_risk_bands_are_sorted_and_backfilled(self):
		bands = [(50.0, "Watch"), (90.0, "Preferred")]  # unsorted, no catch-all
		self.assertEqual(
			engine.score_questionnaire([answer("a", 5)], risk_bands=bands).risk_tier, "Preferred"
		)
		self.assertEqual(engine.score_questionnaire([answer("a", 3)], risk_bands=bands).risk_tier, "Watch")
		self.assertEqual(
			engine.score_questionnaire([answer("a", 1)], risk_bands=bands).risk_tier, models.RISK_HIGH
		)


class TestDocumentEvaluation(unittest.TestCase):
	AS_OF = D("2026-08-13")

	def statuses(self, docs, window=30):
		return {
			status.doc_type: status
			for status in engine.evaluate_documents(docs, self.AS_OF, expiring_within_days=window)
		}

	def test_classification(self):
		result = self.statuses(
			[
				doc("ISO 9001 Certificate", expiry="2027-01-01"),
				doc("Liability Insurance", expiry="2026-08-20"),
				doc("Tax Clearance", expiry="2026-08-12"),
				doc("Bank Letter", provided=False),
				doc("Code of Conduct", expiry=None, requires_expiry=False),
			]
		)
		self.assertEqual(result["ISO 9001 Certificate"].status, models.VALID)
		self.assertEqual(result["ISO 9001 Certificate"].days_to_expiry, 141)
		self.assertEqual(result["Liability Insurance"].status, models.EXPIRING)
		self.assertEqual(result["Liability Insurance"].days_to_expiry, 7)
		self.assertEqual(result["Tax Clearance"].status, models.EXPIRED)
		self.assertEqual(result["Tax Clearance"].days_to_expiry, -1)
		self.assertEqual(result["Bank Letter"].status, models.MISSING)
		self.assertIsNone(result["Bank Letter"].days_to_expiry)
		# attached but no expiry captured -> valid, no expiry
		self.assertEqual(result["Code of Conduct"].status, models.VALID)
		self.assertIsNone(result["Code of Conduct"].days_to_expiry)

	def test_expiring_window_boundaries(self):
		# exactly at the window -> Expiring; one day beyond -> Valid
		result = self.statuses(
			[
				doc("At Window", expiry="2026-09-12"),  # +30
				doc("Past Window", expiry="2026-09-13"),  # +31
				doc("Today", expiry="2026-08-13"),  # +0
			]
		)
		self.assertEqual(result["At Window"].status, models.EXPIRING)
		self.assertEqual(result["Past Window"].status, models.VALID)
		self.assertEqual(result["Today"].status, models.EXPIRING)
		self.assertEqual(result["Today"].days_to_expiry, 0)

	def test_missing_wins_over_expiry_data(self):
		result = self.statuses([doc("Insurance", provided=False, expiry="2027-01-01")])
		self.assertEqual(result["Insurance"].status, models.MISSING)

	def test_string_dates_accepted(self):
		statuses = engine.evaluate_documents(
			[models.DocumentRequirement("ISO", provided=True, expiry_date="2026-08-20")],
			"2026-08-13",
		)
		self.assertEqual(statuses[0].status, models.EXPIRING)
		self.assertEqual(statuses[0].days_to_expiry, 7)

	def test_documents_ok_and_counts(self):
		docs = [
			doc("ISO", expiry="2027-01-01"),
			doc("Insurance", expiry="2026-08-20"),
			doc("Optional Cert", provided=False, mandatory=False),
		]
		statuses = engine.evaluate_documents(docs, self.AS_OF)
		self.assertFalse(engine.documents_ok(statuses))
		self.assertTrue(engine.documents_ok(statuses, mandatory_only=True))
		self.assertEqual(engine.count_expiring(statuses), 1)

	def test_documents_ok_false_on_expired(self):
		statuses = engine.evaluate_documents([doc("ISO", expiry="2026-01-01")], self.AS_OF)
		self.assertFalse(engine.documents_ok(statuses, mandatory_only=True))


class TestBidScoring(unittest.TestCase):
	WEIGHTS: typing.ClassVar[dict] = {
		"price": 50,
		"lead_time": 20,
		"scorecard": 20,
		"qualification": 10,
	}

	def test_price_and_lead_normalization(self):
		lines = [
			bid("Alpha", "WIDGET", qty=10, unit_price=100, lead_time_days=5),
			bid("Beta", "WIDGET", qty=10, unit_price=125, lead_time_days=9),
		]
		scores = {
			s.supplier: s
			for s in engine.score_bids(
				lines, self.WEIGHTS, {"Alpha": 90, "Beta": 70}, {"Alpha": 88, "Beta": 95}
			)
		}

		self.assertAlmostEqual(scores["Alpha"].price_score, 100.0)
		self.assertAlmostEqual(scores["Beta"].price_score, 80.0)  # 100/125
		self.assertAlmostEqual(scores["Alpha"].lead_time_score, 100.0)
		self.assertAlmostEqual(scores["Beta"].lead_time_score, 60.0)  # (5+1)/(9+1)
		# Alpha: (50x100 + 20x100 + 20x90 + 10x88) / 100 = 96.8
		self.assertAlmostEqual(scores["Alpha"].weighted_total, 96.8)
		# Beta: (50x80 + 20x60 + 20x70 + 10x95) / 100 = 75.5
		self.assertAlmostEqual(scores["Beta"].weighted_total, 75.5)
		self.assertEqual(scores["Alpha"].rank, 1)
		self.assertEqual(scores["Beta"].rank, 2)

	def test_missing_scorecard_and_qualification_score_zero_but_ranked(self):
		lines = [
			bid("Alpha", "WIDGET", qty=1, unit_price=100, lead_time_days=0),
			bid("Nobody", "WIDGET", qty=1, unit_price=100, lead_time_days=0),
		]
		scores = {s.supplier: s for s in engine.score_bids(lines, self.WEIGHTS, {"Alpha": 100}, {})}
		self.assertEqual(scores["Nobody"].scorecard_score, 0.0)
		self.assertEqual(scores["Nobody"].qualification_score, 0.0)
		self.assertAlmostEqual(scores["Nobody"].weighted_total, 70.0)  # price+lead only
		self.assertAlmostEqual(scores["Alpha"].weighted_total, 90.0)
		self.assertEqual(len(scores), 2)

	def test_zero_and_negative_price_guards(self):
		lines = [
			bid("Alpha", "WIDGET", qty=1, unit_price=50),
			bid("Broken", "WIDGET", qty=1, unit_price=0),
			bid("Negative", "WIDGET", qty=1, unit_price=-10),
		]
		scores = {s.supplier: s for s in engine.score_bids(lines, self.WEIGHTS)}
		self.assertAlmostEqual(scores["Alpha"].price_score, 100.0)
		self.assertAlmostEqual(scores["Broken"].price_score, 0.0)
		self.assertAlmostEqual(scores["Negative"].price_score, 0.0)

	def test_all_zero_lead_times_score_full(self):
		lines = [
			bid("Alpha", "WIDGET", qty=1, unit_price=100, lead_time_days=0),
			bid("Beta", "WIDGET", qty=1, unit_price=100, lead_time_days=0),
		]
		for score in engine.score_bids(lines, self.WEIGHTS):
			self.assertAlmostEqual(score.lead_time_score, 100.0)

	def test_negative_lead_time_floored(self):
		lines = [
			bid("Alpha", "WIDGET", qty=1, unit_price=100, lead_time_days=-4),
			bid("Beta", "WIDGET", qty=1, unit_price=100, lead_time_days=1),
		]
		scores = {s.supplier: s for s in engine.score_bids(lines, self.WEIGHTS)}
		self.assertAlmostEqual(scores["Alpha"].lead_time_score, 100.0)
		self.assertAlmostEqual(scores["Beta"].lead_time_score, 50.0)  # 1/(1+1)

	def test_qty_weighted_aggregation_across_items(self):
		lines = [
			# Alpha is cheap on the big-qty line, expensive on the small one
			bid("Alpha", "A", qty=100, unit_price=10, lead_time_days=0),
			bid("Alpha", "B", qty=1, unit_price=200, lead_time_days=0),
			bid("Beta", "A", qty=100, unit_price=20, lead_time_days=0),
			bid("Beta", "B", qty=1, unit_price=100, lead_time_days=0),
		]
		scores = {s.supplier: s for s in engine.score_bids(lines, {"price": 100})}
		# Alpha: (100x100 + 1x50)/101 = 99.5 ; Beta: (100x50 + 1x100)/101 = 50.5
		self.assertAlmostEqual(scores["Alpha"].price_score, 99.5, places=2)
		self.assertAlmostEqual(scores["Beta"].price_score, 50.5, places=2)

	def test_zero_qty_falls_back_to_unweighted_mean(self):
		lines = [
			bid("Alpha", "A", qty=0, unit_price=10),
			bid("Alpha", "B", qty=0, unit_price=200),
			bid("Beta", "A", qty=0, unit_price=20),
			bid("Beta", "B", qty=0, unit_price=100),
		]
		scores = {s.supplier: s for s in engine.score_bids(lines, {"price": 100})}
		self.assertAlmostEqual(scores["Alpha"].price_score, 75.0)  # (100 + 50)/2
		self.assertAlmostEqual(scores["Beta"].price_score, 75.0)  # (50 + 100)/2

	def test_rank_tie_break_is_supplier_name(self):
		lines = [
			bid(name, "WIDGET", qty=1, unit_price=100, lead_time_days=2) for name in ("Zeta", "Alpha", "Mid")
		]
		ranked = engine.score_bids(lines, self.WEIGHTS, {"Zeta": 50, "Alpha": 50, "Mid": 50})
		self.assertEqual([s.supplier for s in ranked], ["Alpha", "Mid", "Zeta"])
		self.assertEqual([s.rank for s in ranked], [1, 2, 3])
		# stable across repeated runs / input order
		again = engine.score_bids(list(reversed(lines)), self.WEIGHTS, {"Zeta": 50, "Alpha": 50, "Mid": 50})
		self.assertEqual([s.supplier for s in again], [s.supplier for s in ranked])

	def test_zero_weight_sum_yields_zero_totals(self):
		lines = [bid("Alpha", "WIDGET", qty=1, unit_price=100)]
		scores = engine.score_bids(lines, {"price": 0, "lead_time": 0, "scorecard": 0, "qualification": 0})
		self.assertEqual(scores[0].weighted_total, 0.0)
		self.assertEqual(scores[0].price_score, 100.0)

	def test_scorecard_values_are_clamped(self):
		lines = [bid("Alpha", "WIDGET", qty=1, unit_price=100)]
		scores = engine.score_bids(lines, {"scorecard": 100}, {"Alpha": 140}, {})
		self.assertEqual(scores[0].scorecard_score, 100.0)
		scores = engine.score_bids(lines, {"scorecard": 100}, {"Alpha": -5}, {})
		self.assertEqual(scores[0].scorecard_score, 0.0)


class TestAwardScenarios(unittest.TestCase):
	def setUp(self):
		self.lines = [
			bid("Alpha", "PUMP", qty=10, unit_price=100, lead_time_days=5),
			bid("Alpha", "VALVE", qty=20, unit_price=12, lead_time_days=5),
			bid("Beta", "PUMP", qty=10, unit_price=90, lead_time_days=30),
			bid("Gamma", "VALVE", qty=20, unit_price=15, lead_time_days=2),
		]
		self.scores = engine.score_bids(
			self.lines,
			{"price": 50, "lead_time": 20, "scorecard": 20, "qualification": 10},
			{"Alpha": 95, "Beta": 60, "Gamma": 80},
			{"Alpha": 90, "Beta": 75, "Gamma": 85},
		)

	def scenarios(self, item_codes=None):
		return {
			scenario.name: scenario
			for scenario in engine.build_award_scenarios(self.lines, self.scores, item_codes)
		}

	def test_lowest_price_scenario(self):
		scenario = self.scenarios()[models.SCENARIO_LOWEST_PRICE]
		self.assertEqual(
			scenario.awards,
			[("PUMP", "Beta", 10.0, 90.0), ("VALVE", "Alpha", 20.0, 12.0)],
		)
		self.assertAlmostEqual(scenario.total_cost, 10 * 90 + 20 * 12)
		self.assertEqual(scenario.coverage_gaps, [])

	def test_best_weighted_scenario_prefers_the_stronger_supplier(self):
		by_supplier = {s.supplier: s.weighted_total for s in self.scores}
		self.assertGreater(by_supplier["Alpha"], by_supplier["Beta"])
		scenario = self.scenarios()[models.SCENARIO_BEST_WEIGHTED]
		self.assertEqual(
			scenario.awards,
			[("PUMP", "Alpha", 10.0, 100.0), ("VALVE", "Alpha", 20.0, 12.0)],
		)
		self.assertAlmostEqual(scenario.total_cost, 10 * 100 + 20 * 12)

	def test_single_supplier_picks_widest_coverage(self):
		scenario = self.scenarios()[models.SCENARIO_SINGLE_SUPPLIER]
		self.assertEqual({supplier for _i, supplier, _q, _p in scenario.awards}, {"Alpha"})
		self.assertAlmostEqual(scenario.total_cost, 10 * 100 + 20 * 12)
		self.assertEqual(scenario.coverage_gaps, [])

	def test_single_supplier_tie_breaks_on_cheaper_basket(self):
		lines = [
			bid("Alpha", "PUMP", qty=1, unit_price=100),
			bid("Alpha", "VALVE", qty=1, unit_price=100),
			bid("Beta", "PUMP", qty=1, unit_price=80),
			bid("Beta", "VALVE", qty=1, unit_price=80),
		]
		scenarios = {s.name: s for s in engine.build_award_scenarios(lines, [])}
		single = scenarios[models.SCENARIO_SINGLE_SUPPLIER]
		self.assertEqual({supplier for _i, supplier, _q, _p in single.awards}, {"Beta"})
		self.assertAlmostEqual(single.total_cost, 160.0)

	def test_coverage_gaps_for_unbid_items(self):
		basket = ["PUMP", "VALVE", "GASKET"]
		scenarios = self.scenarios(basket)
		for name in models.SCENARIO_NAMES:
			self.assertIn("GASKET", scenarios[name].coverage_gaps, name)
		# single supplier only covers what Alpha bid
		self.assertEqual(scenarios[models.SCENARIO_SINGLE_SUPPLIER].coverage_gaps, ["GASKET"])

	def test_single_supplier_gaps_when_nobody_covers_everything(self):
		lines = [
			bid("Alpha", "PUMP", qty=1, unit_price=100),
			bid("Beta", "VALVE", qty=1, unit_price=50),
		]
		scenarios = {s.name: s for s in engine.build_award_scenarios(lines, [], ["PUMP", "VALVE"])}
		single = scenarios[models.SCENARIO_SINGLE_SUPPLIER]
		self.assertEqual(len(single.awards), 1)
		# both cover 1 item -> tie-break on the cheaper covered basket (50 < 100)
		self.assertEqual(single.awards[0][1], "Beta")
		self.assertEqual(single.coverage_gaps, ["PUMP"])
		self.assertEqual(scenarios[models.SCENARIO_LOWEST_PRICE].coverage_gaps, [])

	def test_zero_priced_lines_are_not_awardable(self):
		lines = [
			bid("Alpha", "PUMP", qty=1, unit_price=0),
			bid("Beta", "PUMP", qty=1, unit_price=90),
		]
		scenarios = {s.name: s for s in engine.build_award_scenarios(lines, [], ["PUMP"])}
		self.assertEqual(scenarios[models.SCENARIO_LOWEST_PRICE].awards, [("PUMP", "Beta", 1.0, 90.0)])

	def test_no_bids_at_all(self):
		scenarios = {s.name: s for s in engine.build_award_scenarios([], [], ["PUMP", "VALVE"])}
		for name in models.SCENARIO_NAMES:
			self.assertEqual(scenarios[name].awards, [])
			self.assertEqual(scenarios[name].total_cost, 0.0)
			self.assertEqual(scenarios[name].coverage_gaps, ["PUMP", "VALVE"])


if __name__ == "__main__":
	unittest.main(verbosity=2)
