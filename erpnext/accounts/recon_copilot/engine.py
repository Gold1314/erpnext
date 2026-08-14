# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure ranking engine for the bank reconciliation copilot.

Zero frappe imports, no ambient time - callers pass ``today`` explicitly.

The engine does **not** find candidates (the reconciliation tool's SQL in
``bank_reconciliation_tool.check_matching``/``get_*_matching_query`` already
does that); it *ranks* the candidate universe with explainable component
scores so the banking SPA can show one-click suggestions with reasons.

Component scores (each 0-100):

- ``amount_score``   exact amount (within 0.005) -> 100; otherwise a linear
  falloff ``100 x (1 - |delta| / txn.amount)`` floored at 0. Partial-payment
  carve-out: when the candidate is an invoice whose ``outstanding`` is >= the
  transaction amount and the transaction pays that outstanding *exactly*
  (within 0.005), score 90 - strong signal, but deliberately below a
  full-amount match.
- ``date_score``     ``100 - 5 x |days between|`` floored at 0. A candidate
  dated *after* the bank transaction gets an extra flat penalty of 10 points
  (money usually leaves the books before or on the day the bank sees it, so a
  future-dated voucher is slightly suspicious but not disqualifying).
- ``reference_score``  exact reference equality, or the candidate's
  reference_no/bill_no found as a token in the transaction's description /
  reference -> 100; otherwise Jaccard token overlap x 100 (alphanumeric
  tokens >= 3 chars, case-insensitive, pure stopword tokens ignored).
- ``party_score``    exact party-hint equality -> 100; otherwise the best
  ``difflib.SequenceMatcher`` ratio between the normalized party/party_name
  and a sliding token window over the description, x 100.
- ``history_score``  prior reconciliations of the same (party, voucher_type)
  from ``MatchHistory``, with diminishing returns: ``min(count, 5) / 5 x 100``
  (the 6th prior match adds nothing - 5 confirmations are already
  near-certain evidence). A description-prefix hit pointing at the same
  party contributes through the same curve.

The weighted total uses ``models.DEFAULT_WEIGHTS`` (amount .35, reference
.25, party .20, date .10, history .10); custom weights are normalized so the
total stays on the 0-100 scale.

``auto_match_eligible`` is deliberately conservative: total >= 95 AND the
amount is *exactly* equal (the 90-point partial-payment carve-out does NOT
qualify) AND at least one identity signal (reference_score or party_score)
is >= 90. It is a UI flag only - nothing in this package writes a
reconciliation without an explicit human call to ``api.accept_suggestion``.
"""

from __future__ import annotations

import datetime
import re
from difflib import SequenceMatcher

from erpnext.accounts.recon_copilot.models import (
	AMOUNT,
	COMPONENTS,
	DATE,
	DEFAULT_WEIGHTS,
	HISTORY,
	PARTY,
	REFERENCE,
	CandidateVoucher,
	MatchHistory,
	ScoredSuggestion,
	TxnFeatures,
)

#: two amounts within this are "exact" (half of the smallest displayed cent)
EXACT_AMOUNT_TOLERANCE = 0.005

#: extra flat penalty when the candidate voucher is dated after the bank txn
FUTURE_DATE_PENALTY = 10.0

#: minimum alphanumeric length for a reference to count as a substring hit
#: (guards against "42" matching every description containing 42)
MIN_REFERENCE_HIT_LENGTH = 4

#: minimum token length for Jaccard overlap
MIN_TOKEN_LENGTH = 3

#: history saturates after this many prior matches (diminishing returns)
HISTORY_SATURATION = 5

#: length of the normalized description prefix used as a history key
DESCRIPTION_PREFIX_LENGTH = 24

#: auto-match thresholds (see module docstring - conservative by design)
AUTO_MATCH_MIN_SCORE = 95.0
AUTO_MATCH_MIN_IDENTITY = 90.0

#: tokens that are pure noise in bank statement descriptions - never let
#: them create a reference "overlap" on their own
STOPWORDS = frozenset(
	{
		"the",
		"and",
		"for",
		"from",
		"with",
		"ref",
		"reference",
		"pay",
		"payment",
		"paid",
		"transfer",
		"trf",
		"txn",
		"transaction",
		"neft",
		"rtgs",
		"imps",
		"upi",
		"ach",
		"sepa",
		"swift",
		"wire",
		"card",
		"invoice",
		"inv",
		"bill",
		"ltd",
		"llc",
		"inc",
		"gmbh",
		"pvt",
		"limited",
	}
)

_ALNUM_RUN = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def collapse(text: str | None) -> str:
	"""Lowercase and strip everything but letters/digits ("INV-0042 " -> "inv0042")."""
	if not text:
		return ""
	return "".join(_ALNUM_RUN.findall(text.lower()))


def raw_tokens(text: str | None) -> list[str]:
	"""All lowercase alphanumeric runs, in order, no filtering."""
	if not text:
		return []
	return _ALNUM_RUN.findall(text.lower())


def tokenize(text: str | None) -> set[str]:
	"""Jaccard token set: alphanumeric runs >= 3 chars, lowercased, minus stopwords."""
	return {token for token in raw_tokens(text) if len(token) >= MIN_TOKEN_LENGTH and token not in STOPWORDS}


def normalize_name(text: str | None) -> str:
	"""Lowercased, single-spaced alphanumeric words ("Acme, Corp." -> "acme corp")."""
	return " ".join(raw_tokens(text))


def description_prefix(text: str | None) -> str:
	"""Normalized prefix used as the MatchHistory description key.

	Loaders MUST build ``MatchHistory.description_party`` keys with this same
	function so lookups line up.
	"""
	return collapse(text)[:DESCRIPTION_PREFIX_LENGTH]


# ---------------------------------------------------------------------------
# component scores - each returns (score 0-100, human phrase or None)
# ---------------------------------------------------------------------------


def amount_score(txn: TxnFeatures, candidate: CandidateVoucher) -> tuple[float, str | None]:
	if not txn.amount or txn.amount <= 0:
		# zero/negative amount transaction - nothing sane to compare against
		return 0.0, None

	delta = abs((candidate.amount or 0.0) - txn.amount)
	if delta <= EXACT_AMOUNT_TOLERANCE:
		return 100.0, "exact amount"

	if (
		candidate.outstanding is not None
		and candidate.outstanding + EXACT_AMOUNT_TOLERANCE >= txn.amount
		and abs(candidate.outstanding - txn.amount) <= EXACT_AMOUNT_TOLERANCE
	):
		return 90.0, "pays the invoice's outstanding exactly (partial payment)"

	score = max(0.0, 100.0 * (1.0 - delta / txn.amount))
	phrase = f"amount within {min(100.0, delta / txn.amount * 100.0):.1f}%" if score > 0 else None
	return score, phrase


def date_score(
	txn: TxnFeatures, candidate: CandidateVoucher, today: datetime.date | None = None
) -> tuple[float, str | None]:
	anchor = txn.date or today
	if not anchor or not candidate.date:
		return 0.0, None

	days_between = abs((candidate.date - anchor).days)
	score = 100.0 - 5.0 * days_between
	is_future = candidate.date > anchor
	if is_future:
		score -= FUTURE_DATE_PENALTY
	score = max(0.0, score)

	if score <= 0:
		return 0.0, None
	if days_between == 0:
		return score, "same date"
	suffix = " (future-dated)" if is_future else ""
	return score, f"{days_between} day{'s' if days_between != 1 else ''} apart{suffix}"


def reference_score(txn: TxnFeatures, candidate: CandidateVoucher) -> tuple[float, str | None]:
	references = [ref for ref in (candidate.reference_no, candidate.bill_no) if ref and collapse(ref)]
	txn_reference = collapse(txn.reference_number)
	txn_description = collapse(txn.description)

	for reference in references:
		collapsed = collapse(reference)
		if txn_reference and collapsed == txn_reference:
			return 100.0, f"reference {reference.strip()} matches the transaction reference"
		if len(collapsed) >= MIN_REFERENCE_HIT_LENGTH and (
			collapsed in txn_description or (txn_reference and collapsed in txn_reference)
		):
			return 100.0, f"reference {reference.strip()} found in description"

	candidate_tokens = tokenize(" ".join(references))
	txn_tokens = tokenize(f"{txn.description or ''} {txn.reference_number or ''}")
	if not candidate_tokens or not txn_tokens:
		return 0.0, None

	union = candidate_tokens | txn_tokens
	overlap = candidate_tokens & txn_tokens
	jaccard = len(overlap) / len(union) if union else 0.0
	if jaccard <= 0:
		return 0.0, None
	return jaccard * 100.0, f"reference tokens overlap {jaccard * 100.0:.0f}%"


def _best_window_ratio(name_norm: str, text: str | None) -> float:
	"""Best SequenceMatcher ratio of ``name_norm`` against sliding token
	windows (size k-1..k+1, k = token count of the name) over ``text``."""
	if not name_norm or not text:
		return 0.0

	text_tokens = raw_tokens(text)
	if not text_tokens:
		return 0.0

	name_size = max(1, len(name_norm.split()))
	best = 0.0
	for window_size in {max(1, name_size - 1), name_size, name_size + 1}:
		if window_size >= len(text_tokens):
			window_texts = [" ".join(text_tokens)]
		else:
			window_texts = [
				" ".join(text_tokens[start : start + window_size])
				for start in range(len(text_tokens) - window_size + 1)
			]
		for window_text in window_texts:
			ratio = SequenceMatcher(None, name_norm, window_text).ratio()
			if ratio > best:
				best = ratio
	return best


def party_score(txn: TxnFeatures, candidate: CandidateVoucher) -> tuple[float, str | None]:
	names = [name for name in (candidate.party_name, candidate.party) if name and normalize_name(name)]
	if not names:
		return 0.0, None

	hint = normalize_name(txn.party_hint)
	if hint:
		for name in names:
			if normalize_name(name) == hint:
				return 100.0, f"party {name.strip()} matches the transaction party"

	best_ratio, best_name = 0.0, None
	for name in names:
		name_norm = normalize_name(name)
		ratio = max(
			_best_window_ratio(name_norm, txn.description),
			_best_window_ratio(name_norm, txn.party_hint),
		)
		if ratio > best_ratio:
			best_ratio, best_name = ratio, name

	if best_ratio <= 0:
		return 0.0, None
	return (
		best_ratio * 100.0,
		f"party {best_name.strip()} resembles the description ({best_ratio * 100.0:.0f}% similar)",
	)


def history_score(
	txn: TxnFeatures, candidate: CandidateVoucher, history: MatchHistory | None
) -> tuple[float, str | None]:
	if history is None or not candidate.party:
		return 0.0, None

	count = history.party_voucher_counts.get((candidate.party, candidate.voucher_type), 0)

	prefix = description_prefix(txn.description)
	if prefix:
		entry = history.description_party.get(prefix)
		if entry and entry[0] == candidate.party:
			count = max(count, entry[1])

	if count <= 0:
		return 0.0, None

	score = min(count, HISTORY_SATURATION) / HISTORY_SATURATION * 100.0
	return score, f"matched this payer {count} time{'s' if count != 1 else ''} before"


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def resolve_weights(weights: dict[str, float] | None) -> dict[str, float]:
	"""Merge partial overrides over the defaults and normalize to sum 1.0."""
	merged = dict(DEFAULT_WEIGHTS)
	if weights:
		for key, value in weights.items():
			if key in merged:
				merged[key] = max(0.0, float(value))

	total = sum(merged.values())
	if total <= 0:
		return dict(DEFAULT_WEIGHTS)
	return {key: value / total for key, value in merged.items()}


def explain_weights(weights: dict[str, float] | None = None) -> list[dict]:
	"""Weight breakdown for the UI (component, label, normalized weight, %)."""
	labels = {
		AMOUNT: "Amount",
		REFERENCE: "Reference",
		PARTY: "Party",
		DATE: "Date",
		HISTORY: "History",
	}
	resolved = resolve_weights(weights)
	return [
		{
			"component": component,
			"label": labels[component],
			"weight": round(resolved[component], 4),
			"weight_pct": round(resolved[component] * 100.0, 2),
		}
		for component in COMPONENTS
	]


def score_candidates(
	txn: TxnFeatures,
	candidates: list[CandidateVoucher],
	history: MatchHistory | None = None,
	today: datetime.date | None = None,
	weights: dict[str, float] | None = None,
) -> list[ScoredSuggestion]:
	"""Rank ``candidates`` for ``txn``.

	Returns suggestions ordered deterministically: score descending, then
	voucher_name ascending (so equal-score candidates never shuffle between
	runs).
	"""
	resolved_weights = resolve_weights(weights)
	suggestions = []

	for candidate in candidates:
		scored = {
			AMOUNT: amount_score(txn, candidate),
			DATE: date_score(txn, candidate, today),
			REFERENCE: reference_score(txn, candidate),
			PARTY: party_score(txn, candidate),
			HISTORY: history_score(txn, candidate, history),
		}
		features = {component: round(scored[component][0], 2) for component in COMPONENTS}
		total = sum(resolved_weights[component] * scored[component][0] for component in COMPONENTS)

		amount_exact = bool(
			txn.amount
			and txn.amount > 0
			and abs((candidate.amount or 0.0) - txn.amount) <= EXACT_AMOUNT_TOLERANCE
		)
		auto_match_eligible = (
			total >= AUTO_MATCH_MIN_SCORE
			and amount_exact
			and (features[REFERENCE] >= AUTO_MATCH_MIN_IDENTITY or features[PARTY] >= AUTO_MATCH_MIN_IDENTITY)
		)

		suggestions.append(
			ScoredSuggestion(
				candidate=candidate,
				score=round(total, 2),
				features=features,
				explanation=_build_explanation(scored, resolved_weights),
				auto_match_eligible=auto_match_eligible,
			)
		)

	suggestions.sort(key=lambda suggestion: (-suggestion.score, suggestion.candidate.voucher_name))
	return suggestions


def _build_explanation(
	scored: dict[str, tuple[float, str | None]], weights: dict[str, float], top_n: int = 3
) -> str:
	"""Top contributing components, in words, ordered by weighted contribution."""
	contributions = [
		(weights[component] * score, phrase)
		for component, (score, phrase) in scored.items()
		if score > 0 and phrase
	]
	if not contributions:
		return "no meaningful signals"

	contributions.sort(key=lambda item: -item[0])
	return "; ".join(phrase for _, phrase in contributions[:top_n])
