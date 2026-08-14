# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure anomaly-detection checks over accounting data.

Zero frappe imports and no ambient time - every check is a deterministic
function of its inputs, returning ``list[Finding]``. Loaders
(``loaders.py``) fetch the data; the scanner (``scanner.py``) persists the
findings. Math is stdlib only (``difflib``, ``math``), keeping the repo's
no-numpy discipline (see GAP_CLOSURE_BLUEPRINT.md section 2).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re

from erpnext.accounts.anomaly.models import (
	CHECK_ACCOUNT_OUTLIER,
	CHECK_BENFORD_DEVIATION,
	CHECK_DUPLICATE_INVOICE,
	CHECK_RARE_COMBINATION,
	CHECK_SUSPICIOUS_POSTING,
	HIGH,
	LOW,
	MEDIUM,
	Finding,
	GLMovement,
	InvoiceRecord,
	PostingRecord,
)

# ---------------------------------------------------------------------------
# Tunables (mirrored as defaults in Anomaly Detection Settings)
# ---------------------------------------------------------------------------

#: invoice amounts within this % of each other count as "the same amount"
DUPLICATE_AMOUNT_TOLERANCE_PCT = 0.5
#: difflib ratio at or above which two bill numbers count as "similar"
BILL_NO_SIMILARITY_THRESHOLD = 0.85
#: |z| at or above which an outlier is High instead of Medium
OUTLIER_HIGH_Z = 4.5
#: per-signal scores for suspicious postings (they stack)
ROUND_AMOUNT_SCORE = 1.0
WEEKEND_POSTING_SCORE = 1.0
BACKDATED_SCORE = 1.5

#: chi-square critical value at significance 0.01 for 8 degrees of freedom
#: (9 first-digit bins - 1). A Benford scope is only flagged when its
#: chi-square statistic exceeds this, i.e. the observed first-digit
#: distribution would arise from a Benford-distributed population less than
#: 1% of the time. Standard table value: chi2(0.99, df=8) = 20.090.
BENFORD_CHI2_CRITICAL_0_01 = 20.09

#: Benford's law: P(first digit = d) = log10(1 + 1/d)
BENFORD_EXPECTED = {d: math.log10(1.0 + 1.0 / d) for d in range(1, 10)}


# ---------------------------------------------------------------------------
# Check 1: duplicate supplier invoices
# ---------------------------------------------------------------------------


def normalize_bill_no(bill_no: str | None) -> str:
	"""Canonical form of a supplier bill number.

	Lower-cases, drops all non-alphanumeric characters (spaces, dashes,
	slashes, dots) and strips leading zeros from every digit run, so
	"INV-001", "inv 001" and "INV0001" all normalize to "inv1".
	"""
	if not bill_no:
		return ""
	normalized = re.sub(r"[^a-z0-9]", "", bill_no.lower())
	return re.sub(r"\d+", lambda m: m.group().lstrip("0") or "0", normalized)


def bill_no_similarity(a: str, b: str) -> float:
	"""difflib ratio between two already-normalized bill numbers."""
	if not a or not b:
		return 0.0
	return difflib.SequenceMatcher(None, a, b).ratio()


def amounts_match(a: float, b: float, tolerance_pct: float = DUPLICATE_AMOUNT_TOLERANCE_PCT) -> bool:
	"""True when the two amounts differ by at most ``tolerance_pct`` percent
	of the larger absolute amount."""
	reference = max(abs(a), abs(b))
	if reference == 0:
		return True
	return abs(a - b) <= reference * tolerance_pct / 100.0


def find_duplicate_invoices(
	invoices: list[InvoiceRecord],
	days_window: int = 45,
	amount_tolerance_pct: float = DUPLICATE_AMOUNT_TOLERANCE_PCT,
	similarity_threshold: float = BILL_NO_SIMILARITY_THRESHOLD,
) -> list[Finding]:
	"""Screen supplier invoices for likely duplicates.

	A pair is a candidate only when both invoices share the supplier, the
	amounts match within ``amount_tolerance_pct`` and the effective dates
	(bill date, falling back to posting date) lie within ``days_window``
	days. Then:

	- equal normalized bill numbers -> High (score 1.0)
	- similar bill numbers (difflib ratio >= ``similarity_threshold``)
	  -> High (score = ratio)
	- at least one bill number empty -> Medium (amount+date only, score 0.5)
	- two different, dissimilar bill numbers -> not flagged (presumed to be
	  genuinely distinct bills)

	Each pair is reported exactly once: invoices are pre-sorted by
	(effective date, name) so the pair (A, B) is emitted in stable order and
	the mirrored (B, A) never appears.
	"""
	findings = []

	by_supplier: dict[str, list[InvoiceRecord]] = {}
	for invoice in invoices:
		by_supplier.setdefault(invoice.supplier, []).append(invoice)

	for supplier in sorted(by_supplier):
		group = sorted(by_supplier[supplier], key=lambda inv: (str(inv.effective_date), inv.name))
		for i in range(len(group)):
			for j in range(i + 1, len(group)):
				first, second = group[i], group[j]
				finding = _screen_invoice_pair(
					first, second, days_window, amount_tolerance_pct, similarity_threshold
				)
				if finding:
					findings.append(finding)

	return findings


def _screen_invoice_pair(
	first: InvoiceRecord,
	second: InvoiceRecord,
	days_window: int,
	amount_tolerance_pct: float,
	similarity_threshold: float,
) -> Finding | None:
	if abs((second.effective_date - first.effective_date).days) > days_window:
		return None
	if not amounts_match(first.amount, second.amount, amount_tolerance_pct):
		return None

	bill_a = normalize_bill_no(first.bill_no)
	bill_b = normalize_bill_no(second.bill_no)

	if bill_a and bill_b:
		if bill_a == bill_b:
			match_type, severity, score, similarity = "bill_no_equal", HIGH, 1.0, 1.0
		else:
			similarity = bill_no_similarity(bill_a, bill_b)
			if similarity < similarity_threshold:
				return None
			match_type, severity, score = "bill_no_similar", HIGH, round(similarity, 4)
	else:
		match_type, severity, score, similarity = "amount_and_date", MEDIUM, 0.5, 0.0

	return Finding(
		check_key=CHECK_DUPLICATE_INVOICE,
		severity=severity,
		entity_type="Purchase Invoice",
		entity_name=second.name,
		message=(
			f"Possible duplicate of {first.name}: same supplier {first.supplier}, "
			f"amount {second.amount:,.2f} vs {first.amount:,.2f}, "
			f"bill no {second.bill_no or '(empty)'} vs {first.bill_no or '(empty)'} ({match_type})"
		),
		score=score,
		details={
			"invoice_a": first.name,
			"invoice_b": second.name,
			"supplier": first.supplier,
			"bill_no_a": first.bill_no or "",
			"bill_no_b": second.bill_no or "",
			"amount_a": first.amount,
			"amount_b": second.amount,
			"date_a": str(first.effective_date),
			"date_b": str(second.effective_date),
			"match_type": match_type,
			"similarity": round(similarity, 4),
		},
	)


# ---------------------------------------------------------------------------
# Check 2: off-trend account movement (z-score)
# ---------------------------------------------------------------------------


def find_account_outliers(
	movements_by_account: dict[str, list[GLMovement]],
	min_history: int = 6,
	z_threshold: float = 3.0,
	high_z: float = OUTLIER_HIGH_Z,
) -> list[Finding]:
	"""Flag accounts whose latest month's net movement is off-trend.

	For each account with at least ``min_history`` *prior* months of
	history, the latest month's net (debit - credit) is compared against the
	mean/population-stddev of the prior months. Zero stddev (perfectly flat
	history) is skipped - any deviation from a constant series would be an
	infinite z. |z| >= ``z_threshold`` raises a finding: High when
	|z| >= ``high_z``, else Medium; score = |z|.
	"""
	findings = []

	for account in sorted(movements_by_account):
		movements = sorted(movements_by_account[account], key=lambda m: m.period)
		if len(movements) < min_history + 1:
			continue

		latest = movements[-1]
		history = [m.net for m in movements[:-1]]

		mean = sum(history) / len(history)
		variance = sum((value - mean) ** 2 for value in history) / len(history)
		stddev = math.sqrt(variance)
		if stddev == 0:
			continue

		z = (latest.net - mean) / stddev
		if abs(z) < z_threshold:
			continue

		findings.append(
			Finding(
				check_key=CHECK_ACCOUNT_OUTLIER,
				severity=HIGH if abs(z) >= high_z else MEDIUM,
				entity_type="Account",
				entity_name=account,
				message=(
					f"Net movement of {account} in {latest.period} ({latest.net:,.2f}) is "
					f"{abs(z):.1f} standard deviations from its {len(history)}-month "
					f"average ({mean:,.2f})"
				),
				score=round(abs(z), 4),
				details={
					"period": latest.period,
					"net": round(latest.net, 2),
					"mean": round(mean, 2),
					"stddev": round(stddev, 2),
					"z": round(z, 4),
					"history_months": len(history),
				},
			)
		)

	return findings


# ---------------------------------------------------------------------------
# Check 3: rare account/dimension combinations
# ---------------------------------------------------------------------------


def find_rare_combinations(
	combo_counts: dict[tuple[str, str], int],
	total_by_account: dict[str, int],
	min_account_postings: int = 20,
	rarity_pct: float = 1.0,
	dimension_label: str = "cost center",
) -> list[Finding]:
	"""Flag dimension values an account almost never posts with.

	A combination (account, dimension value) is rare when the account has at
	least ``min_account_postings`` postings overall and the combination
	accounts for at most ``rarity_pct`` percent of them. Medium when the
	share is at most half of ``rarity_pct``, else Low; score =
	``rarity_pct - share_pct`` (the rarer, the higher).
	"""
	findings = []

	for account, dimension_value in sorted(combo_counts):
		count = combo_counts[(account, dimension_value)]
		total = total_by_account.get(account, 0)
		if total < min_account_postings or count <= 0:
			continue

		share_pct = 100.0 * count / total
		if share_pct > rarity_pct:
			continue

		findings.append(
			Finding(
				check_key=CHECK_RARE_COMBINATION,
				severity=MEDIUM if share_pct <= rarity_pct / 2.0 else LOW,
				entity_type="Account",
				entity_name=account,
				message=(
					f"Account {account} rarely posts with {dimension_label} "
					f"{dimension_value or '(empty)'}: {count} of {total} postings "
					f"({share_pct:.2f}%)"
				),
				score=round(rarity_pct - share_pct, 4),
				details={
					"dimension_value": dimension_value or "",
					"dimension_label": dimension_label,
					"count": count,
					"total_postings": total,
					"share_pct": round(share_pct, 4),
				},
			)
		)

	return findings


# ---------------------------------------------------------------------------
# Check 4: suspicious postings (round / weekend / backdated - signals stack)
# ---------------------------------------------------------------------------


def find_suspicious_postings(
	postings: list[PostingRecord],
	round_amount_threshold: float = 10000,
	backdate_days_threshold: int = 14,
) -> list[Finding]:
	"""Screen individual postings for classic fraud-adjacent traits.

	Signals per posting (scores stack into one finding):

	- round amount: whole multiple of 1000 and >= ``round_amount_threshold``
	  -> +1.0 (Low on its own)
	- weekend posting of a manual Journal Entry (weekday 5/6) -> +1.0
	  (Low on its own)
	- backdated by >= ``backdate_days_threshold`` days (posting date vs
	  record creation) -> +1.5 (Medium)

	Severity is Medium when the posting is backdated or shows two or more
	signals, otherwise Low.
	"""
	findings = []

	for posting in postings:
		flags = []
		score = 0.0

		amount = abs(posting.amount)
		if amount >= round_amount_threshold and amount == int(amount) and int(amount) % 1000 == 0:
			flags.append("round_amount")
			score += ROUND_AMOUNT_SCORE

		if posting.weekday in (5, 6) and posting.voucher_type == "Journal Entry":
			flags.append("weekend_posting")
			score += WEEKEND_POSTING_SCORE

		if posting.is_backdated_days >= backdate_days_threshold:
			flags.append("backdated")
			score += BACKDATED_SCORE

		if not flags:
			continue

		severity = MEDIUM if "backdated" in flags or len(flags) >= 2 else LOW
		findings.append(
			Finding(
				check_key=CHECK_SUSPICIOUS_POSTING,
				severity=severity,
				entity_type=posting.voucher_type,
				entity_name=posting.voucher_no,
				message=(
					f"{posting.voucher_type} {posting.voucher_no} "
					f"({posting.amount:,.2f} on {posting.posting_date}) flagged: "
					+ ", ".join(flag.replace("_", " ") for flag in flags)
				),
				score=round(score, 4),
				details={
					"flags": flags,
					"amount": posting.amount,
					"posting_date": str(posting.posting_date),
					"weekday": posting.weekday,
					"backdated_days": posting.is_backdated_days,
					"created_by": posting.created_by or "",
					"account": posting.account or "",
				},
			)
		)

	return findings


# ---------------------------------------------------------------------------
# Check 5: Benford first-digit deviation
# ---------------------------------------------------------------------------


def first_digit(amount: float) -> int | None:
	"""Leading significant digit of ``amount`` (1-9), or None for zero."""
	amount = abs(amount)
	if amount == 0:
		return None
	while amount < 1:
		amount *= 10
	while amount >= 10:
		amount /= 10
	return int(amount)


def find_benford_deviation(
	amounts: list[float],
	min_n: int = 300,
	scope: str = "All",
	chi2_critical: float = BENFORD_CHI2_CRITICAL_0_01,
) -> list[Finding]:
	"""Chi-square test of first-digit frequencies against Benford's law.

	Only runs with at least ``min_n`` usable (non-zero) amounts - below
	that, first-digit tests produce too many false alarms to act on. Flags
	the scope with one Medium finding when the chi-square statistic exceeds
	``chi2_critical`` (default 20.09 = the 0.01 critical value for 8 degrees
	of freedom, i.e. 9 digit bins - 1); score = the statistic. ``scope`` is
	whatever slice the caller tested (e.g. a voucher type or "All").
	"""
	observed = dict.fromkeys(range(1, 10), 0)
	for amount in amounts:
		digit = first_digit(amount)
		if digit is not None:
			observed[digit] += 1

	n = sum(observed.values())
	if n < min_n:
		return []

	chi2 = 0.0
	deviations = []
	for digit in range(1, 10):
		expected = n * BENFORD_EXPECTED[digit]
		contribution = (observed[digit] - expected) ** 2 / expected
		chi2 += contribution
		deviations.append((contribution, digit, observed[digit], expected))

	if chi2 <= chi2_critical:
		return []

	top = sorted(deviations, reverse=True)[:3]
	return [
		Finding(
			check_key=CHECK_BENFORD_DEVIATION,
			severity=MEDIUM,
			entity_type="Voucher Type" if scope != "All" else "Company",
			entity_name=scope,
			message=(
				f"First-digit distribution for scope '{scope}' deviates from Benford's law "
				f"(chi-square {chi2:.1f} > {chi2_critical} over {n} amounts); "
				"most deviating digits: " + ", ".join(str(d[1]) for d in top)
			),
			score=round(chi2, 4),
			details={
				"scope": scope,
				"n": n,
				"chi2": round(chi2, 4),
				"chi2_critical": chi2_critical,
				"top_digits": [
					{
						"digit": digit,
						"observed": obs,
						"expected": round(expected, 2),
						"contribution": round(contribution, 4),
					}
					for contribution, digit, obs, expected in top
				],
				"observed": {str(d): observed[d] for d in range(1, 10)},
			},
		)
	]


# ---------------------------------------------------------------------------
# Fingerprinting & dedupe
# ---------------------------------------------------------------------------

#: detail keys that identify a finding (vs. keys that merely describe it and
#: may drift between scans, like scores or percentages). Fallback: no detail
#: keys, i.e. identity = check + entity only.
IDENTITY_DETAIL_KEYS = {
	CHECK_DUPLICATE_INVOICE: ("invoice_a", "invoice_b"),
	CHECK_ACCOUNT_OUTLIER: ("period",),
	CHECK_RARE_COMBINATION: ("dimension_value",),
	CHECK_SUSPICIOUS_POSTING: ("flags",),
	CHECK_BENFORD_DEVIATION: (),
}


def compute_fingerprint(finding: Finding) -> str:
	"""Stable identity hash of a finding: check_key + entity + the
	identity-carrying subset of details (see :data:`IDENTITY_DETAIL_KEYS`).

	Volatile detail values (scores, z values, percentages) are deliberately
	excluded so a rescan re-identifies the same open finding even when the
	numbers moved slightly.
	"""
	parts = [finding.check_key, finding.entity_type, finding.entity_name]
	for key in IDENTITY_DETAIL_KEYS.get(finding.check_key, ()):
		parts.append(f"{key}={json.dumps(finding.details.get(key), sort_keys=True, default=str)}")
	return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def dedupe_against_known(findings: list[Finding], known_keys: set[str]) -> list[Finding]:
	"""Drop findings whose fingerprint is already known (open findings from
	earlier scans) or repeated within this batch; return only new ones."""
	seen = set(known_keys)
	new_findings = []
	for finding in findings:
		fingerprint = compute_fingerprint(finding)
		if fingerprint in seen:
			continue
		seen.add(fingerprint)
		new_findings.append(finding)
	return new_findings
