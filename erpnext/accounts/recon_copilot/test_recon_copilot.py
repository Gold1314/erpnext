# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure reconciliation-copilot ranking engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/recon_copilot/test_recon_copilot.py

- the modules are loaded directly from their file paths under their canonical
names (same bootstrap as ``erpnext/accounts/forecasting/test_engine.py``).
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
	from erpnext.accounts.recon_copilot import engine, models
except Exception:
	# stand-alone run: register stub packages so engine.py's absolute import
	# of erpnext.accounts.recon_copilot.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.recon_copilot"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.recon_copilot.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.recon_copilot.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


TODAY = D("2026-08-13")


def txn(amount=1000.0, date="2026-08-10", description=None, reference_number=None, party_hint=None):
	return models.TxnFeatures(
		amount=amount,
		date=D(date) if isinstance(date, str) else date,
		description=description,
		reference_number=reference_number,
		party_hint=party_hint,
	)


def cand(name="PE-0001", voucher_type="Payment Entry", amount=1000.0, date="2026-08-10", **kwargs):
	return models.CandidateVoucher(
		voucher_type=voucher_type,
		voucher_name=name,
		amount=amount,
		date=D(date) if isinstance(date, str) else date,
		**kwargs,
	)


class TestAmountScore(unittest.TestCase):
	def test_exact_amount_within_half_cent(self):
		score, phrase = engine.amount_score(txn(amount=1000.0), cand(amount=1000.004))
		self.assertEqual(score, 100.0)
		self.assertEqual(phrase, "exact amount")

	def test_linear_falloff_floored_at_zero(self):
		score, _ = engine.amount_score(txn(amount=1000.0), cand(amount=900.0))
		self.assertAlmostEqual(score, 90.0)
		score, _ = engine.amount_score(txn(amount=100.0), cand(amount=350.0))
		self.assertEqual(score, 0.0)

	def test_partial_payment_carve_out(self):
		# invoice for 2000 with 500 still outstanding; bank txn pays exactly 500
		invoice = cand(name="ACC-SINV-0001", voucher_type="Sales Invoice", amount=2000.0, outstanding=500.0)
		score, phrase = engine.amount_score(txn(amount=500.0), invoice)
		self.assertEqual(score, 90.0)
		self.assertIn("partial payment", phrase)

	def test_carve_out_needs_exact_outstanding(self):
		invoice = cand(voucher_type="Sales Invoice", amount=2000.0, outstanding=499.9)
		score, _ = engine.amount_score(txn(amount=500.0), invoice)
		# falls back to the linear falloff against the voucher amount
		self.assertAlmostEqual(score, 0.0)  # 100 x (1 - 1500/500) floored

	def test_zero_amount_guard(self):
		# no ZeroDivisionError, amount contributes nothing
		score, phrase = engine.amount_score(txn(amount=0.0), cand(amount=100.0))
		self.assertEqual((score, phrase), (0.0, None))
		suggestions = engine.score_candidates(txn(amount=0.0), [cand()], today=TODAY)
		self.assertEqual(suggestions[0].features[models.AMOUNT], 0.0)


class TestDateScore(unittest.TestCase):
	def test_five_points_per_day_floored(self):
		score, _ = engine.date_score(txn(date="2026-08-10"), cand(date="2026-08-04"))
		self.assertEqual(score, 70.0)
		score, _ = engine.date_score(txn(date="2026-08-10"), cand(date="2025-08-10"))
		self.assertEqual(score, 0.0)

	def test_future_dated_candidate_gets_extra_penalty(self):
		past, _ = engine.date_score(txn(date="2026-08-10"), cand(date="2026-08-08"))
		future, phrase = engine.date_score(txn(date="2026-08-10"), cand(date="2026-08-12"))
		self.assertEqual(past, 90.0)
		self.assertEqual(future, 80.0)  # 90 - FUTURE_DATE_PENALTY
		self.assertIn("future-dated", phrase)

	def test_missing_dates_score_zero(self):
		score, _ = engine.date_score(txn(date=None), cand(date="2026-08-10"))
		self.assertEqual(score, 0.0)
		score, _ = engine.date_score(txn(date="2026-08-10"), cand(date=None))
		self.assertEqual(score, 0.0)


class TestTokenization(unittest.TestCase):
	def test_short_tokens_ignored(self):
		self.assertEqual(engine.tokenize("AB 12 x INV-0042"), {"0042"})

	def test_case_insensitive(self):
		self.assertEqual(engine.tokenize("ACME Acme acme"), {"acme"})

	def test_punctuation_splits_tokens(self):
		self.assertEqual(engine.tokenize("wire:ACME/0042-2026"), {"acme", "0042", "2026"})

	def test_stopwords_dropped(self):
		self.assertEqual(engine.tokenize("payment for the invoice transfer"), set())


class TestReferenceScore(unittest.TestCase):
	def test_reference_equality(self):
		score, phrase = engine.reference_score(
			txn(reference_number="INV-0042"), cand(reference_no="inv 0042")
		)
		self.assertEqual(score, 100.0)
		self.assertIn("matches the transaction reference", phrase)

	def test_reference_found_in_description(self):
		score, phrase = engine.reference_score(
			txn(description="SEPA CREDIT INV-0042 ACME"), cand(reference_no="INV-0042")
		)
		self.assertEqual(score, 100.0)
		self.assertIn("INV-0042", phrase)

	def test_bill_no_found_in_description(self):
		score, _ = engine.reference_score(txn(description="wire BILL/7781 settlement"), cand(bill_no="7781"))
		self.assertEqual(score, 100.0)

	def test_short_reference_does_not_substring_match(self):
		# "42" appears inside the description, but 2 alnum chars is too weak
		score, _ = engine.reference_score(txn(description="charge 3420042"), cand(reference_no="42"))
		self.assertEqual(score, 0.0)

	def test_jaccard_overlap(self):
		score, phrase = engine.reference_score(
			txn(description="ORDER alpha beta gamma"), cand(reference_no="alpha delta")
		)
		# cand tokens {alpha, delta}, txn tokens {order, alpha, beta, gamma}
		# overlap 1 / union 5 = 20%
		self.assertAlmostEqual(score, 20.0)
		self.assertIn("20%", phrase)

	def test_no_tokens_no_score(self):
		score, _ = engine.reference_score(txn(description="something"), cand(reference_no="AB 12"))
		self.assertEqual(score, 0.0)


class TestPartyScore(unittest.TestCase):
	def test_party_hint_equality(self):
		score, _ = engine.party_score(
			txn(party_hint="ACME Corp"), cand(party="CUST-001", party_name="Acme, Corp.")
		)
		self.assertEqual(score, 100.0)

	def test_fuzzy_window_against_description(self):
		score, phrase = engine.party_score(
			txn(description="SEPA CREDIT ACME CORP INV 99"), cand(party="Acme Corp", party_name="Acme Corp")
		)
		self.assertEqual(score, 100.0)  # perfect token window "acme corp"
		self.assertIn("Acme Corp", phrase)

	def test_partial_similarity(self):
		score, _ = engine.party_score(
			txn(description="payment ACME industries"), cand(party_name="Acme Industry GmbH")
		)
		self.assertGreater(score, 60.0)
		self.assertLess(score, 100.0)

	def test_no_party_no_score(self):
		score, _ = engine.party_score(txn(description="anything"), cand())
		self.assertEqual(score, 0.0)


class TestHistoryScore(unittest.TestCase):
	def make_history(self, count):
		return models.MatchHistory(party_voucher_counts={("CUST-001", "Payment Entry"): count})

	def test_diminishing_returns(self):
		candidate = cand(party="CUST-001")
		for count, expected in ((1, 20.0), (3, 60.0), (5, 100.0), (7, 100.0)):
			score, _ = engine.history_score(txn(), candidate, self.make_history(count))
			self.assertEqual(score, expected, f"count={count}")

	def test_phrase_reports_real_count_even_when_capped(self):
		_, phrase = engine.history_score(txn(), cand(party="CUST-001"), self.make_history(7))
		self.assertIn("7 times", phrase)

	def test_description_prefix_evidence(self):
		description = "GYM MEMBERSHIP DIRECT DEBIT JULY"
		history = models.MatchHistory(
			description_party={engine.description_prefix(description): ("SUPP-009", 4)}
		)
		score, _ = engine.history_score(
			txn(description=description), cand(party="SUPP-009", voucher_type="Payment Entry"), history
		)
		self.assertEqual(score, 80.0)

	def test_no_history_or_party(self):
		self.assertEqual(engine.history_score(txn(), cand(party="CUST-001"), None)[0], 0.0)
		self.assertEqual(engine.history_score(txn(), cand(party=None), self.make_history(3))[0], 0.0)


class TestRanking(unittest.TestCase):
	def test_exact_amount_beats_closer_date_fuzzy_amount(self):
		exact_but_older = cand(name="PE-EXACT", amount=1000.0, date="2026-07-31")  # 10 days off
		close_date_fuzzy_amount = cand(name="PE-FUZZY", amount=800.0, date="2026-08-10")  # same day
		suggestions = engine.score_candidates(
			txn(amount=1000.0, date="2026-08-10"),
			[close_date_fuzzy_amount, exact_but_older],
			today=TODAY,
		)
		self.assertEqual([s.candidate.voucher_name for s in suggestions], ["PE-EXACT", "PE-FUZZY"])

	def test_reference_token_match_beats_party_only(self):
		with_reference = cand(name="PE-REF", amount=700.0, reference_no="INV-0042")
		party_only = cand(name="PE-PARTY", amount=700.0, party="Acme", party_name="Acme")
		suggestions = engine.score_candidates(
			txn(amount=1000.0, date="2026-08-10", description="PAYMENT INV-0042 ACME"),
			[party_only, with_reference],
			today=TODAY,
		)
		self.assertEqual(suggestions[0].candidate.voucher_name, "PE-REF")
		self.assertEqual(suggestions[0].features[models.REFERENCE], 100.0)

	def test_deterministic_tie_break_on_voucher_name(self):
		twins = [cand(name="PE-B"), cand(name="PE-A")]
		for ordering in (twins, list(reversed(twins))):
			suggestions = engine.score_candidates(txn(), ordering, today=TODAY)
			self.assertEqual(suggestions[0].score, suggestions[1].score)
			self.assertEqual([s.candidate.voucher_name for s in suggestions], ["PE-A", "PE-B"])

	def test_empty_candidates(self):
		self.assertEqual(engine.score_candidates(txn(), [], today=TODAY), [])

	def test_explanation_lists_top_components(self):
		suggestion = engine.score_candidates(
			txn(amount=1000.0, date="2026-08-10", reference_number="INV-7"),
			[cand(amount=1000.0, date="2026-08-10", reference_no="INV-7")],
			today=TODAY,
		)[0]
		self.assertIn("exact amount", suggestion.explanation)
		self.assertIn("reference", suggestion.explanation)


class TestAutoMatchEligibility(unittest.TestCase):
	def build(self, amount=1000.0, date="2026-08-08", history_count=3, **cand_kwargs):
		history = models.MatchHistory(party_voucher_counts={("CUST-001", "Payment Entry"): history_count})
		return engine.score_candidates(
			txn(
				amount=1000.0,
				date="2026-08-10",
				reference_number="INV-0042",
				party_hint="Acme Corp",
			),
			[
				cand(
					amount=amount,
					date=date,
					reference_no="INV-0042",
					party="CUST-001",
					party_name="Acme Corp",
					**cand_kwargs,
				)
			],
			history=history,
			today=TODAY,
		)[0]

	def test_exactly_95_with_exact_amount_and_reference_is_eligible(self):
		# amount 100 (.35) + ref 100 (.25) + party 100 (.20) + date 90 (.10) + history 60 (.10)
		suggestion = self.build()
		self.assertEqual(suggestion.score, 95.0)
		self.assertTrue(suggestion.auto_match_eligible)

	def test_just_below_95_is_not_eligible(self):
		# one more day of distance: date 85 -> total 94.5, everything else identical
		suggestion = self.build(date="2026-08-07")
		self.assertEqual(suggestion.score, 94.5)
		self.assertFalse(suggestion.auto_match_eligible)

	def test_high_score_without_exact_amount_is_not_eligible(self):
		# partial-payment carve-out (amount 90): total 96.5 but amount not exact
		suggestion = self.build(amount=2000.0, outstanding=1000.0, date="2026-08-10", history_count=5)
		self.assertEqual(suggestion.score, 96.5)
		self.assertFalse(suggestion.auto_match_eligible)

	def test_high_score_without_identity_signal_is_not_eligible(self):
		# skew weights so amount+date alone clear 95 while ref/party stay < 90
		weights = {
			models.AMOUNT: 0.65,
			models.DATE: 0.3,
			models.REFERENCE: 0.03,
			models.PARTY: 0.01,
			models.HISTORY: 0.01,
		}
		suggestion = engine.score_candidates(
			txn(amount=1000.0, date="2026-08-10", description="alpha beta"),
			[cand(amount=1000.0, date="2026-08-10", reference_no="alpha gamma delta")],
			today=TODAY,
			weights=weights,
		)[0]
		self.assertGreaterEqual(suggestion.score, 95.0)
		self.assertLess(suggestion.features[models.REFERENCE], 90.0)
		self.assertLess(suggestion.features[models.PARTY], 90.0)
		self.assertFalse(suggestion.auto_match_eligible)


class TestWeights(unittest.TestCase):
	def test_defaults_sum_to_one(self):
		self.assertAlmostEqual(sum(models.DEFAULT_WEIGHTS.values()), 1.0)

	def test_partial_override_is_normalized(self):
		resolved = engine.resolve_weights({models.AMOUNT: 0.7})
		self.assertAlmostEqual(sum(resolved.values()), 1.0)
		self.assertGreater(resolved[models.AMOUNT], models.DEFAULT_WEIGHTS[models.AMOUNT])

	def test_explain_weights(self):
		rows = engine.explain_weights()
		self.assertEqual([row["component"] for row in rows], list(models.COMPONENTS))
		self.assertAlmostEqual(sum(row["weight_pct"] for row in rows), 100.0)


if __name__ == "__main__":
	unittest.main(verbosity=2)
