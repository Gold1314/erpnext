# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure consolidation engine.

``models.py`` / ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/consolidation/test_consolidation.py

- the modules are loaded directly from their file paths under their canonical
names (bootstrap pattern borrowed from
``erpnext/accounts/closing/test_sequencing.py``).
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
	from erpnext.accounts.consolidation import engine, models
except Exception:
	# stand-alone run: register stub packages so the modules load without
	# importing erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.consolidation"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.consolidation.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.consolidation.engine", _HERE / "engine.py")

CompanyBalance = models.CompanyBalance
OwnershipEdge = models.OwnershipEdge
EliminationPair = models.EliminationPair
TranslationRates = models.TranslationRates


def edge(parent, sub, percent, method="Full"):
	return OwnershipEdge(parent=parent, subsidiary=sub, percent=percent, method=method)


def balanced_company(company, abbr, cash):
	"""A tiny balanced trial balance: cash funded by capital + current profit.

	Debit-positive: Cash +, Share Capital -, Sales -, Rent +.
	Sums to zero by construction (capital is solved as the plug).
	"""
	rows = [
		CompanyBalance(company, f"Cash - {abbr}", "Asset", cash),
		CompanyBalance(company, f"Sales - {abbr}", "Income", -40.0),
		CompanyBalance(company, f"Rent - {abbr}", "Expense", 10.0),
	]
	capital = -sum(r.balance for r in rows)
	rows.append(CompanyBalance(company, f"Share Capital - {abbr}", "Equity", capital))
	return rows


class TestResolveGroup(unittest.TestCase):
	def test_chain_percents_multiply(self):
		edges = [edge("P", "A", 80), edge("A", "B", 60)]
		members = engine.resolve_group("P", edges)
		self.assertEqual(set(members), {"A", "B"})
		self.assertAlmostEqual(members["A"].percent, 80.0)
		self.assertAlmostEqual(members["B"].percent, 48.0)
		self.assertEqual(members["B"].method, "Full")

	def test_parallel_paths_sum_capped_at_100(self):
		# P owns 60% of C directly and another 100%*50% via A
		edges = [edge("P", "A", 100), edge("P", "C", 60), edge("A", "C", 50)]
		members = engine.resolve_group("P", edges)
		self.assertAlmostEqual(members["C"].percent, 100.0)  # 60 + 50, capped

	def test_cycle_raises(self):
		edges = [edge("P", "A", 80), edge("A", "B", 60), edge("B", "A", 10)]
		with self.assertRaises(ValueError):
			engine.resolve_group("P", edges)

	def test_self_ownership_raises(self):
		with self.assertRaises(ValueError):
			engine.resolve_group("P", [edge("P", "P", 100)])

	def test_equity_method_stops_descent(self):
		edges = [edge("P", "A", 30, method="Equity"), edge("A", "B", 100)]
		members = engine.resolve_group("P", edges)
		self.assertEqual(members["A"].method, "Equity")
		self.assertNotIn("B", members)

	def test_unrelated_edges_ignored(self):
		edges = [edge("P", "A", 80), edge("X", "Y", 100)]
		members = engine.resolve_group("P", edges)
		self.assertEqual(set(members), {"A"})


class TestTranslate(unittest.TestCase):
	def test_identity_rates_no_cta(self):
		balances = balanced_company("Sub", "S", cash=100.0)
		translated, cta = engine.translate(balances, {"Sub": TranslationRates(1.0, 1.0)})
		self.assertAlmostEqual(sum(b.balance for b in translated), 0.0)
		self.assertAlmostEqual(cta["Sub"], 0.0)
		self.assertFalse(any(b.account == models.CTA_ACCOUNT for b in translated))

	def test_split_rates_column_balances_via_cta_plug(self):
		balances = balanced_company("Sub", "S", cash=100.0)
		rates = {"Sub": TranslationRates(closing_rate=2.0, average_rate=1.5)}
		translated, cta = engine.translate(balances, rates)

		# each translated column must still sum to zero
		total = sum(b.balance for b in translated if b.company == "Sub")
		self.assertAlmostEqual(total, 0.0)

		cta_rows = [b for b in translated if b.account == models.CTA_ACCOUNT]
		self.assertEqual(len(cta_rows), 1)
		self.assertEqual(cta_rows[0].root_type, "Equity")
		self.assertAlmostEqual(cta_rows[0].balance, cta["Sub"])

		# P&L: sales -40, rent +10 -> net -30 local; at average 1.5 = -45,
		# at closing it would be -60, so the plug is the -15 difference.
		self.assertAlmostEqual(cta["Sub"], -15.0)

	def test_bs_at_closing_pl_at_average(self):
		balances = [
			CompanyBalance("Sub", "Cash - S", "Asset", 10.0),
			CompanyBalance("Sub", "Sales - S", "Income", -10.0),
		]
		rates = {"Sub": TranslationRates(closing_rate=3.0, average_rate=2.0)}
		translated, _cta = engine.translate(balances, rates)
		by_account = {b.account: b.balance for b in translated}
		self.assertAlmostEqual(by_account["Cash - S"], 30.0)
		self.assertAlmostEqual(by_account["Sales - S"], -20.0)


class TestEliminate(unittest.TestCase):
	def pair(self, rule="IC AR/AP"):
		return EliminationPair(
			company_a="A",
			account_a="IC Receivable - A",
			company_b="B",
			account_b="IC Payable - B",
			rule=rule,
		)

	def test_matched_pair_fully_eliminated_no_exception(self):
		balances = [
			CompanyBalance("A", "IC Receivable - A", "Asset", 100.0),
			CompanyBalance("B", "IC Payable - B", "Liability", -100.0),
		]
		adjusted, lines, exceptions = engine.eliminate(balances, [self.pair()])
		self.assertEqual(exceptions, [])
		self.assertEqual(len(lines), 2)
		self.assertAlmostEqual(sum(line.amount for line in lines), 0.0)

		net = {}
		for b in adjusted:
			net[(b.company, b.account)] = net.get((b.company, b.account), 0.0) + b.balance
		self.assertAlmostEqual(net[("A", "IC Receivable - A")], 0.0)
		self.assertAlmostEqual(net[("B", "IC Payable - B")], 0.0)

	def test_residual_reported_never_plugged(self):
		balances = [
			CompanyBalance("A", "IC Receivable - A", "Asset", 100.0),
			CompanyBalance("B", "IC Payable - B", "Liability", -80.0),
		]
		adjusted, lines, exceptions = engine.eliminate(balances, [self.pair()])
		# min magnitude (80) eliminated on both sides
		self.assertEqual(len(lines), 2)
		self.assertAlmostEqual(sum(line.amount for line in lines), 0.0)
		amounts = sorted(line.amount for line in lines)
		self.assertAlmostEqual(amounts[0], -80.0)
		self.assertAlmostEqual(amounts[1], 80.0)

		net = {}
		for b in adjusted:
			net[(b.company, b.account)] = net.get((b.company, b.account), 0.0) + b.balance
		self.assertAlmostEqual(net[("A", "IC Receivable - A")], 20.0)  # residual stays
		self.assertAlmostEqual(net[("B", "IC Payable - B")], 0.0)

		self.assertEqual(len(exceptions), 1)
		self.assertIn("100.00", exceptions[0])
		self.assertIn("-80.00", exceptions[0])
		self.assertIn("20.00", exceptions[0])

	def test_same_sign_balances_flagged_not_eliminated(self):
		balances = [
			CompanyBalance("A", "IC Receivable - A", "Asset", 100.0),
			CompanyBalance("B", "IC Payable - B", "Liability", 50.0),
		]
		_adjusted, lines, exceptions = engine.eliminate(balances, [self.pair()])
		self.assertEqual(lines, [])
		self.assertEqual(len(exceptions), 1)
		self.assertIn("do not offset", exceptions[0])

	def test_zero_pair_skipped(self):
		_adjusted, lines, exceptions = engine.eliminate([], [self.pair()])
		self.assertEqual(lines, [])
		self.assertEqual(exceptions, [])


class TestMinorityInterest(unittest.TestCase):
	def test_eighty_percent_sub(self):
		group = {"Sub": models.GroupMember("Sub", 80.0, "Full")}
		balances = [
			# equity credit 1000
			CompanyBalance("Sub", "Share Capital - S", "Equity", -1000.0),
			# net income credit 200 (income 300 credit, expense 100 debit)
			CompanyBalance("Sub", "Sales - S", "Income", -300.0),
			CompanyBalance("Sub", "Rent - S", "Expense", 100.0),
			# assets do not affect MI
			CompanyBalance("Sub", "Cash - S", "Asset", 1200.0),
		]
		mi_by_company, total = engine.minority_interest(balances, group)
		self.assertAlmostEqual(mi_by_company["Sub"], 0.2 * (1000.0 + 200.0))
		self.assertAlmostEqual(total, 240.0)

	def test_wholly_owned_and_equity_method_excluded(self):
		group = {
			"Whole": models.GroupMember("Whole", 100.0, "Full"),
			"Assoc": models.GroupMember("Assoc", 30.0, "Equity"),
		}
		balances = [
			CompanyBalance("Whole", "Share Capital - W", "Equity", -500.0),
			CompanyBalance("Assoc", "Share Capital - X", "Equity", -500.0),
		]
		mi_by_company, total = engine.minority_interest(balances, group)
		self.assertEqual(mi_by_company, {})
		self.assertAlmostEqual(total, 0.0)


class TestConsolidate(unittest.TestCase):
	"""Full pipeline on a 3-company fixture (parent + 100% sub + 80% sub)."""

	def fixture(self):
		abbrs = ["PC", "SA", "SB"]
		balances = []
		# Parent (PC): holds an IC receivable from SB
		balances += balanced_company("Parent Co", "PC", cash=500.0)
		balances.append(CompanyBalance("Parent Co", "IC Receivable - PC", "Asset", 120.0))
		balances.append(CompanyBalance("Parent Co", "Share Capital - PC", "Equity", -120.0))
		# Sub A (SA): wholly owned, same currency
		balances += balanced_company("Sub A", "SA", cash=200.0)
		# Sub B (SB): 80% owned, foreign currency, owes the parent
		balances += balanced_company("Sub B", "SB", cash=300.0)
		balances.append(CompanyBalance("Sub B", "IC Payable - SB", "Liability", -60.0))
		balances.append(CompanyBalance("Sub B", "Cash - SB", "Asset", 60.0))

		edges = [edge("Parent Co", "Sub A", 100), edge("Parent Co", "Sub B", 80)]
		rates = {
			"Parent Co": TranslationRates(1.0, 1.0),
			"Sub A": TranslationRates(1.0, 1.0),
			"Sub B": TranslationRates(2.0, 1.5),
		}
		pairs = [
			EliminationPair(
				company_a="Parent Co",
				account_a="IC Receivable - PC",
				company_b="Sub B",
				account_b="IC Payable - SB",
				rule="IC AR/AP",
			)
		]
		return balances, edges, rates, pairs, abbrs

	def test_consolidated_trial_balance_sums_to_zero(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		self.assertAlmostEqual(sum(result.consolidated.values()), 0.0, places=6)
		# every translated company column balances too
		for company, column in result.columns.items():
			self.assertAlmostEqual(sum(column.values()), 0.0, places=6, msg=company)

	def test_elimination_and_residual_exception(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		# SB owes 60 local = 120 at closing rate 2.0, matching the parent's 120
		self.assertEqual(len(result.elimination_lines), 2)
		self.assertEqual(result.exceptions, [])
		self.assertAlmostEqual(result.consolidated.get("IC Receivable", 0.0), 0.0)
		self.assertAlmostEqual(result.consolidated.get("IC Payable", 0.0), 0.0)

	def test_minority_interest_math(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		# Sub B local: capital credit 330 (funds cash 360 incl. IC-funded 60,
		# net income 30), translated: equity = closing-rate BS plug story —
		# recompute independently from the result's own column:
		column = result.columns["Sub B"]
		equity_credit = -sum(
			amount for key, amount in column.items() if result.root_type_by_key.get(key) == "Equity"
		)
		net_income_credit = -sum(
			amount
			for key, amount in column.items()
			if result.root_type_by_key.get(key) in ("Income", "Expense")
		)
		expected = 0.2 * (equity_credit + net_income_credit)
		self.assertAlmostEqual(result.minority_interest_by_company["Sub B"], expected)
		self.assertAlmostEqual(result.minority_interest_total, expected)
		self.assertGreater(expected, 0.0)
		# MI presented as a net-zero reclass inside equity
		self.assertAlmostEqual(
			result.consolidated[models.MINORITY_INTEREST_ACCOUNT]
			+ result.consolidated[models.MI_RECLASS_ACCOUNT],
			0.0,
		)

	def test_cta_present_only_for_foreign_sub(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		self.assertAlmostEqual(result.cta_by_company["Parent Co"], 0.0)
		self.assertAlmostEqual(result.cta_by_company["Sub A"], 0.0)
		self.assertNotEqual(result.cta_by_company["Sub B"], 0.0)
		self.assertIn(models.CTA_ACCOUNT, result.columns["Sub B"])

	def test_account_merging_across_abbrs(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		self.assertIn("Cash", result.consolidated)
		self.assertNotIn("Cash - PC", result.consolidated)
		self.assertNotIn("Cash - SB", result.consolidated)
		# Cash = 500 (PC) + 200 (SA) + 360 local * 2.0 closing (SB)
		self.assertAlmostEqual(result.consolidated["Cash"], 500.0 + 200.0 + 360.0 * 2.0)

	def test_equity_method_sub_excluded_with_warning(self):
		balances, edges, rates, pairs, abbrs = self.fixture()
		edges.append(edge("Parent Co", "Assoc Co", 30, method="Equity"))
		balances.append(CompanyBalance("Assoc Co", "Cash - AC", "Asset", 999.0))
		result = engine.consolidate("Parent Co", balances, edges, rates, pairs, abbrs)
		self.assertNotIn("Assoc Co", result.companies)
		self.assertTrue(any("Equity method" in w for w in result.warnings))
		self.assertNotIn("Cash - AC", result.consolidated)


class TestStripAbbr(unittest.TestCase):
	def test_strip_and_longest_wins(self):
		strip = models.strip_company_abbr
		self.assertEqual(strip("Cash - PC", ["PC", "SA"]), "Cash")
		self.assertEqual(strip("Cash", ["PC"]), "Cash")
		# "C" must not chew into " - ABC"
		self.assertEqual(strip("Cash - ABC", ["C", "ABC"]), "Cash")
		# synthetic accounts pass through untouched
		self.assertEqual(strip(models.CTA_ACCOUNT, ["PC"]), models.CTA_ACCOUNT)


if __name__ == "__main__":
	unittest.main(verbosity=2)
