# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure anomaly-detection engine.

``models.py`` and ``engine.py`` have no frappe dependency, but importing
them through the ``erpnext`` package would pull in ``erpnext/__init__.py``
(which imports frappe). So when run as a plain file -

	python erpnext/accounts/anomaly/test_anomaly.py

- the modules are loaded directly from their file paths under their
canonical names, keeping the engine's ``from
erpnext.accounts.anomaly.models import ...`` working without a site (same
bootstrap as erpnext/accounts/forecasting/test_engine.py).
"""

from __future__ import annotations

import datetime
import importlib.util
import math
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
	from erpnext.accounts.anomaly import engine, models
except Exception:
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.anomaly"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.anomaly.models", _HERE / "models.py")
	engine = _load_module("erpnext.accounts.anomaly.engine", _HERE / "engine.py")


def D(iso: str) -> datetime.date:
	return datetime.date.fromisoformat(iso)


def invoice(name, supplier="Acme", bill_no=None, bill_date=None, amount=1000.0, posting_date="2026-08-01"):
	return models.InvoiceRecord(
		name=name,
		supplier=supplier,
		bill_no=bill_no,
		bill_date=D(bill_date) if bill_date else None,
		amount=amount,
		posting_date=D(posting_date),
	)


def movement(account, period, net):
	return models.GLMovement(account=account, period=period, total_debit=net, total_credit=0.0)


def posting(
	voucher_no="JV-001",
	voucher_type="Journal Entry",
	amount=500.0,
	posting_date="2026-08-03",
	weekday=0,
	is_backdated_days=0,
	created_by=None,
):
	return models.PostingRecord(
		voucher_type=voucher_type,
		voucher_no=voucher_no,
		account=None,
		amount=amount,
		posting_date=D(posting_date),
		weekday=weekday,
		created_by=created_by,
		is_backdated_days=is_backdated_days,
	)


class TestBillNoNormalization(unittest.TestCase):
	def test_dashes_spaces_case_and_leading_zeros_normalize_equal(self):
		self.assertEqual(engine.normalize_bill_no("INV-001"), "inv1")
		self.assertEqual(engine.normalize_bill_no("inv 001"), "inv1")
		self.assertEqual(engine.normalize_bill_no("INV0001"), "inv1")

	def test_all_zero_run_survives_as_single_zero(self):
		self.assertEqual(engine.normalize_bill_no("A-000"), "a0")

	def test_empty_and_none(self):
		self.assertEqual(engine.normalize_bill_no(None), "")
		self.assertEqual(engine.normalize_bill_no(""), "")


class TestDuplicateInvoices(unittest.TestCase):
	def test_normalized_equal_bill_no_is_high(self):
		findings = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="INV-001", bill_date="2026-08-01"),
				invoice("PI-2", bill_no="inv 001", bill_date="2026-08-10"),
			]
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.HIGH)
		self.assertEqual(findings[0].details["match_type"], "bill_no_equal")
		self.assertEqual(findings[0].score, 1.0)

	def test_leading_zero_variant_matches(self):
		findings = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="INV-001"),
				invoice("PI-2", bill_no="INV0001"),
			]
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].details["match_type"], "bill_no_equal")

	def test_similarity_above_threshold_is_high_with_ratio_score(self):
		# "invoice12345" vs "invoice12346": ratio = 2*11/24 = 0.9167 >= 0.85
		findings = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="INVOICE-12345"),
				invoice("PI-2", bill_no="INVOICE-12346"),
			]
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.HIGH)
		self.assertEqual(findings[0].details["match_type"], "bill_no_similar")
		self.assertAlmostEqual(findings[0].score, 11.0 / 12.0, places=4)

	def test_similarity_below_threshold_not_flagged(self):
		# "ab12" vs "xy89": ratio 0.0 - different bills, same amount/date
		findings = engine.find_duplicate_invoices(
			[invoice("PI-1", bill_no="AB-12"), invoice("PI-2", bill_no="XY-89")]
		)
		self.assertEqual(findings, [])

	def test_empty_bill_no_amount_and_date_is_medium(self):
		findings = engine.find_duplicate_invoices(
			[invoice("PI-1", bill_no=None), invoice("PI-2", bill_no="")]
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.MEDIUM)
		self.assertEqual(findings[0].details["match_type"], "amount_and_date")

	def test_one_empty_bill_no_falls_back_to_amount_and_date(self):
		findings = engine.find_duplicate_invoices(
			[invoice("PI-1", bill_no="INV-9"), invoice("PI-2", bill_no=None)]
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.MEDIUM)

	def test_amount_tolerance_boundary(self):
		# 0.5% of 1005.00 = 5.025 -> 1000.00 vs 1005.00 matches
		flagged = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="B1", amount=1000.0),
				invoice("PI-2", bill_no="B-1", amount=1005.0),
			]
		)
		self.assertEqual(len(flagged), 1)

		# 1000 vs 1010 differs by 1% -> no match
		unflagged = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="B1", amount=1000.0),
				invoice("PI-2", bill_no="B-1", amount=1010.0),
			]
		)
		self.assertEqual(unflagged, [])

	def test_date_window(self):
		outside = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="B1", bill_date="2026-01-01"),
				invoice("PI-2", bill_no="B1", bill_date="2026-04-01"),
			],
			days_window=45,
		)
		self.assertEqual(outside, [])

		inside = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="B1", bill_date="2026-01-01"),
				invoice("PI-2", bill_no="B1", bill_date="2026-02-14"),
			],
			days_window=45,
		)
		self.assertEqual(len(inside), 1)

	def test_different_suppliers_never_pair(self):
		findings = engine.find_duplicate_invoices(
			[
				invoice("PI-1", supplier="Acme", bill_no="B1"),
				invoice("PI-2", supplier="Globex", bill_no="B1"),
			]
		)
		self.assertEqual(findings, [])

	def test_no_mirror_pairs_and_stable_ordering(self):
		invoices = [
			invoice("PI-2", bill_no="B1", posting_date="2026-08-05"),
			invoice("PI-1", bill_no="B1", posting_date="2026-08-01"),
		]
		findings = engine.find_duplicate_invoices(invoices)
		findings_reversed = engine.find_duplicate_invoices(list(reversed(invoices)))

		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].details["invoice_a"], "PI-1")
		self.assertEqual(findings[0].details["invoice_b"], "PI-2")
		# input order must not change the reported pair
		self.assertEqual(findings[0].details, findings_reversed[0].details)

	def test_three_way_duplicates_report_each_pair_once(self):
		findings = engine.find_duplicate_invoices(
			[
				invoice("PI-1", bill_no="B1", posting_date="2026-08-01"),
				invoice("PI-2", bill_no="B1", posting_date="2026-08-02"),
				invoice("PI-3", bill_no="B1", posting_date="2026-08-03"),
			]
		)
		pairs = {(f.details["invoice_a"], f.details["invoice_b"]) for f in findings}
		self.assertEqual(pairs, {("PI-1", "PI-2"), ("PI-1", "PI-3"), ("PI-2", "PI-3")})


class TestAccountOutliers(unittest.TestCase):
	PERIODS = tuple(f"2025-{m:02d}" for m in range(1, 11))  # 10 prior months

	def series(self, nets, latest_net, account="5110 - Freight - AC"):
		movements = [movement(account, period, net) for period, net in zip(self.PERIODS, nets, strict=True)]
		movements.append(movement(account, "2025-11", latest_net))
		return {account: movements}

	def test_known_z_score_medium(self):
		# priors: mean 10, population variance 2 -> std sqrt(2)
		nets = [10, 12, 8, 11, 9, 10, 12, 8, 11, 9]
		findings = engine.find_account_outliers(self.series(nets, 15.0))
		self.assertEqual(len(findings), 1)
		expected_z = 5.0 / math.sqrt(2.0)
		self.assertAlmostEqual(findings[0].score, expected_z, places=3)
		self.assertEqual(findings[0].severity, models.MEDIUM)  # 3.54 < 4.5
		self.assertEqual(findings[0].details["period"], "2025-11")

	def test_high_severity_above_high_z(self):
		nets = [10, 12, 8, 11, 9, 10, 12, 8, 11, 9]
		findings = engine.find_account_outliers(self.series(nets, 17.0))
		self.assertEqual(len(findings), 1)
		self.assertAlmostEqual(findings[0].score, 7.0 / math.sqrt(2.0), places=3)  # 4.95
		self.assertEqual(findings[0].severity, models.HIGH)

	def test_below_threshold_no_finding(self):
		nets = [10, 12, 8, 11, 9, 10, 12, 8, 11, 9]
		self.assertEqual(engine.find_account_outliers(self.series(nets, 12.0)), [])

	def test_zero_stddev_guard(self):
		nets = [100.0] * 10
		self.assertEqual(engine.find_account_outliers(self.series(nets, 5000.0)), [])

	def test_insufficient_history_skipped(self):
		movements = [movement("A", f"2025-0{m}", 10.0 * m) for m in range(1, 6)]
		movements.append(movement("A", "2025-06", 99999.0))
		self.assertEqual(engine.find_account_outliers({"A": movements}, min_history=6), [])


class TestRareCombinations(unittest.TestCase):
	def test_rare_combination_flagged(self):
		findings = engine.find_rare_combinations(
			combo_counts={("5110 - Freight - AC", "CC-South"): 1},
			total_by_account={"5110 - Freight - AC": 200},
		)
		self.assertEqual(len(findings), 1)
		# 0.5% <= 1.0/2 -> Medium
		self.assertEqual(findings[0].severity, models.MEDIUM)
		self.assertEqual(findings[0].details["share_pct"], 0.5)
		self.assertIn("rarely posts with cost center CC-South", findings[0].message)

	def test_low_severity_between_half_and_full_rarity(self):
		findings = engine.find_rare_combinations(
			combo_counts={("A", "CC-1"): 2},
			total_by_account={"A": 250},  # 0.8% -> Low (between 0.5 and 1.0)
		)
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.LOW)

	def test_common_combination_not_flagged(self):
		findings = engine.find_rare_combinations(
			combo_counts={("A", "CC-1"): 5},
			total_by_account={"A": 200},  # 2.5% > 1.0%
		)
		self.assertEqual(findings, [])

	def test_thin_account_history_not_flagged(self):
		findings = engine.find_rare_combinations(
			combo_counts={("A", "CC-1"): 1},
			total_by_account={"A": 10},  # < 20 postings
		)
		self.assertEqual(findings, [])


class TestSuspiciousPostings(unittest.TestCase):
	def test_round_amount_low(self):
		findings = engine.find_suspicious_postings([posting(amount=10000.0)])
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.LOW)
		self.assertEqual(findings[0].details["flags"], ["round_amount"])
		self.assertEqual(findings[0].score, 1.0)

	def test_round_amount_below_threshold_ignored(self):
		self.assertEqual(engine.find_suspicious_postings([posting(amount=9000.0)]), [])

	def test_non_round_amount_ignored(self):
		self.assertEqual(engine.find_suspicious_postings([posting(amount=10500.0)]), [])

	def test_weekend_journal_entry_low(self):
		findings = engine.find_suspicious_postings([posting(amount=500.0, weekday=6)])
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.LOW)
		self.assertEqual(findings[0].details["flags"], ["weekend_posting"])

	def test_weekend_non_journal_voucher_not_flagged(self):
		findings = engine.find_suspicious_postings(
			[posting(voucher_type="Payment Entry", amount=500.0, weekday=6)]
		)
		self.assertEqual(findings, [])

	def test_backdated_medium(self):
		findings = engine.find_suspicious_postings([posting(amount=500.0, is_backdated_days=20)])
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.MEDIUM)
		self.assertEqual(findings[0].score, 1.5)

	def test_backdated_below_threshold_ignored(self):
		self.assertEqual(engine.find_suspicious_postings([posting(amount=500.0, is_backdated_days=13)]), [])

	def test_signals_stack(self):
		findings = engine.find_suspicious_postings([posting(amount=50000.0, weekday=5, is_backdated_days=30)])
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].details["flags"], ["round_amount", "weekend_posting", "backdated"])
		self.assertEqual(findings[0].score, 3.5)
		self.assertEqual(findings[0].severity, models.MEDIUM)

	def test_two_low_signals_escalate_to_medium(self):
		findings = engine.find_suspicious_postings([posting(amount=25000.0, weekday=6)])
		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0].severity, models.MEDIUM)
		self.assertEqual(findings[0].score, 2.0)


class TestBenford(unittest.TestCase):
	def benford_sample(self, n=1000):
		"""Amounts whose first digits follow Benford counts closely."""
		amounts = []
		for digit in range(1, 10):
			count = round(n * engine.BENFORD_EXPECTED[digit])
			amounts.extend([float(digit * 10)] * count)
		return amounts

	def uniform_sample(self, per_digit=100):
		amounts = []
		for digit in range(1, 10):
			amounts.extend([float(digit * 100)] * per_digit)
		return amounts

	def test_conforming_sample_not_flagged(self):
		self.assertEqual(engine.find_benford_deviation(self.benford_sample()), [])

	def test_uniform_digits_flagged(self):
		findings = engine.find_benford_deviation(self.uniform_sample(), scope="Journal Entry")
		self.assertEqual(len(findings), 1)
		finding = findings[0]
		self.assertEqual(finding.severity, models.MEDIUM)
		self.assertEqual(finding.entity_name, "Journal Entry")
		self.assertGreater(finding.score, engine.BENFORD_CHI2_CRITICAL_0_01)
		# digit 1 is the most under-represented -> top deviator
		self.assertEqual(finding.details["top_digits"][0]["digit"], 1)
		self.assertEqual(finding.details["n"], 900)

	def test_min_n_boundary(self):
		# 299 uniform amounts: below min_n -> silent even though deviant
		short = [float((i % 9 + 1) * 10) for i in range(299)]
		self.assertEqual(engine.find_benford_deviation(short, min_n=300), [])

		# exactly 300: runs and flags the uniform distribution
		exact = [float((i % 9 + 1) * 10) for i in range(300)]
		self.assertEqual(len(engine.find_benford_deviation(exact, min_n=300)), 1)

	def test_zero_amounts_do_not_count_toward_n(self):
		sample = [0.0] * 500 + [float((i % 9 + 1) * 10) for i in range(200)]
		self.assertEqual(engine.find_benford_deviation(sample, min_n=300), [])

	def test_first_digit_extraction(self):
		self.assertEqual(engine.first_digit(0.042), 4)
		self.assertEqual(engine.first_digit(7.0), 7)
		self.assertEqual(engine.first_digit(9312.55), 9)
		self.assertEqual(engine.first_digit(-250.0), 2)
		self.assertIsNone(engine.first_digit(0.0))


class TestFingerprintAndDedupe(unittest.TestCase):
	def make_finding(self, **overrides):
		defaults = dict(
			check_key=models.CHECK_DUPLICATE_INVOICE,
			severity=models.HIGH,
			entity_type="Purchase Invoice",
			entity_name="PI-2",
			message="msg",
			score=1.0,
			details={"invoice_a": "PI-1", "invoice_b": "PI-2", "similarity": 1.0},
		)
		defaults.update(overrides)
		return models.Finding(**defaults)

	def test_fingerprint_stable_across_volatile_details(self):
		a = self.make_finding()
		b = self.make_finding(
			message="different message",
			score=0.9,
			details={"invoice_a": "PI-1", "invoice_b": "PI-2", "similarity": 0.87},
		)
		self.assertEqual(engine.compute_fingerprint(a), engine.compute_fingerprint(b))

	def test_fingerprint_differs_for_different_identity(self):
		a = self.make_finding()
		b = self.make_finding(details={"invoice_a": "PI-1", "invoice_b": "PI-3"}, entity_name="PI-3")
		self.assertNotEqual(engine.compute_fingerprint(a), engine.compute_fingerprint(b))

	def test_outlier_identity_includes_period(self):
		base = dict(
			check_key=models.CHECK_ACCOUNT_OUTLIER,
			severity=models.MEDIUM,
			entity_type="Account",
			entity_name="A",
			message="m",
			score=3.2,
		)
		october = models.Finding(details={"period": "2025-10", "z": 3.2}, **base)
		november = models.Finding(details={"period": "2025-11", "z": 3.2}, **base)
		self.assertNotEqual(engine.compute_fingerprint(october), engine.compute_fingerprint(november))

	def test_dedupe_drops_known_and_intra_batch_repeats(self):
		first = self.make_finding()
		repeat = self.make_finding(score=0.99)
		fresh = self.make_finding(entity_name="PI-9", details={"invoice_a": "PI-8", "invoice_b": "PI-9"})
		known = {engine.compute_fingerprint(first)}

		new = engine.dedupe_against_known([first, repeat, fresh], known)
		self.assertEqual(len(new), 1)
		self.assertIs(new[0], fresh)

	def test_dedupe_with_no_known_keys_keeps_all_unique(self):
		first = self.make_finding()
		fresh = self.make_finding(entity_name="PI-9", details={"invoice_a": "PI-8", "invoice_b": "PI-9"})
		new = engine.dedupe_against_known([first, fresh], set())
		self.assertEqual(len(new), 2)


if __name__ == "__main__":
	unittest.main(verbosity=2)
