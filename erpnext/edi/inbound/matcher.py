# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Invoice-line ↔ Purchase-Order-line matching.

Pure domain layer: **no frappe imports** — only ``difflib``. The caller
(``erpnext.edi.inbound.api``) loads candidate Purchase Order lines as plain
dicts and hands them here; nothing in this module knows what a Purchase Order
document looks like.

Why match at all
----------------
ERPNext already does three-way matching once a Purchase Invoice line carries
``purchase_order``/``po_detail`` links. The job here is only to *find* those
links from a document that a supplier wrote without knowledge of our row
names, and to surface the price/quantity variances that a human should look
at before the draft is created.

Strategy, strongest evidence first
----------------------------------
1. ``item_code`` — the supplier echoed our item code in
   ``BuyersItemIdentification`` (BT-156). Unambiguous, so it runs first
   across all lines before weaker evidence gets a chance to claim a PO line.
2. ``order_line_ref`` — ``OrderLineReference/LineID`` (BT-132) names the
   ordered line number.
3. ``description`` — normalized fuzzy comparison at
   :data:`DESCRIPTION_THRESHOLD`; the weakest evidence, so it only gets the
   PO lines the first two passes did not claim.
4. ``unmatched`` — no candidate; the operator resolves it by hand.

Each PO line is claimed by at most one invoice line, and the whole function
is deterministic: passes run in a fixed order, invoice lines in document
order, and ties between equally-good fuzzy candidates break on PO line
position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

#: minimum SequenceMatcher ratio for a description-based match
DESCRIPTION_THRESHOLD = 0.85

METHOD_ITEM_CODE = "item_code"
METHOD_ORDER_LINE_REF = "order_line_ref"
METHOD_DESCRIPTION = "description"
METHOD_UNMATCHED = "unmatched"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class MatchResult:
	"""One invoice line and the PO line it was (or was not) matched to."""

	parsed_line: Any
	po_line: dict | None
	method: str
	price_variance_pct: float = 0.0
	qty_variance_pct: float = 0.0
	within_tolerance: bool = False

	@property
	def matched(self) -> bool:
		return self.po_line is not None


def normalize(text: str | None) -> str:
	"""Lower-case, strip punctuation, collapse whitespace.

	Makes ``"Widget, blue (deluxe)"`` and ``"widget blue deluxe"`` compare
	equal, so supplier formatting differences do not defeat the fuzzy pass.
	"""
	return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def similarity(a: str | None, b: str | None) -> float:
	"""Normalized description similarity in ``[0.0, 1.0]``."""
	left, right = normalize(a), normalize(b)
	if not left or not right:
		return 0.0
	return SequenceMatcher(None, left, right).ratio()


def _variance_pct(actual: float, expected: float) -> float:
	"""Signed percentage difference of ``actual`` against ``expected``.

	A zero baseline cannot yield a percentage: equal values are 0% apart,
	anything else is reported as a full 100% deviation so it trips every
	sane tolerance instead of dividing by zero.
	"""
	if expected:
		return (actual - expected) / expected * 100.0
	return 0.0 if not actual else 100.0


def _line_text(parsed_line) -> str:
	return f"{parsed_line.description or ''} {parsed_line.item_name or ''}".strip()


def _po_text(po_line: dict) -> str:
	return f"{po_line.get('description') or ''} {po_line.get('item_code') or ''}".strip()


def _po_idx(po_line: dict, position: int) -> str:
	"""The ordered line number: the PO row's own ``idx`` when supplied,
	otherwise its 1-based position in the candidate list."""
	idx = po_line.get("idx")
	return str(idx) if idx not in (None, "") else str(position + 1)


def _evaluate(parsed_line, po_line: dict | None, method: str, tolerance_pct: float) -> MatchResult:
	if po_line is None:
		return MatchResult(parsed_line, None, METHOD_UNMATCHED, 0.0, 0.0, False)

	price_variance = _variance_pct(float(parsed_line.unit_price or 0), float(po_line.get("rate") or 0))
	qty_variance = _variance_pct(float(parsed_line.qty or 0), float(po_line.get("qty") or 0))
	within = abs(price_variance) <= tolerance_pct and abs(qty_variance) <= tolerance_pct

	return MatchResult(parsed_line, po_line, method, price_variance, qty_variance, within)


def match_lines(parsed_lines, po_lines, tolerance_pct: float = 2.0) -> list[MatchResult]:
	"""Match invoice lines against candidate PO lines.

	Args:
	    parsed_lines: :class:`~erpnext.edi.inbound.models.ParsedLine` objects
	        in document order.
	    po_lines: dicts with ``item_code``, ``description``, ``qty``,
	        ``rate``, ``po_name``, ``po_detail``, ``remaining_qty`` and
	        optionally ``idx``.
	    tolerance_pct: allowed absolute price/quantity variance, in percent.

	Returns:
	    one :class:`MatchResult` per invoice line, in invoice line order.
	"""
	po_lines = list(po_lines)
	claimed: set[int] = set()
	assignment: dict[int, tuple[int, str]] = {}  # invoice line index -> (po index, method)

	def unclaimed() -> list[tuple[int, dict]]:
		return [(i, po) for i, po in enumerate(po_lines) if i not in claimed]

	# --- pass 1: buyer item code == PO item code (exact) ---
	for line_index, line in enumerate(parsed_lines):
		code = (line.buyer_item_code or "").strip()
		if not code:
			continue
		for po_index, po_line in unclaimed():
			if (po_line.get("item_code") or "").strip() == code:
				assignment[line_index] = (po_index, METHOD_ITEM_CODE)
				claimed.add(po_index)
				break

	# --- pass 2: order line reference == ordered line number ---
	for line_index, line in enumerate(parsed_lines):
		if line_index in assignment:
			continue
		ref = (line.order_line_ref or "").strip()
		if not ref:
			continue
		for po_index, po_line in unclaimed():
			if _po_idx(po_line, po_index) == ref:
				assignment[line_index] = (po_index, METHOD_ORDER_LINE_REF)
				claimed.add(po_index)
				break

	# --- pass 3: fuzzy description ---
	for line_index, line in enumerate(parsed_lines):
		if line_index in assignment:
			continue
		best_index, best_score = None, 0.0
		for po_index, po_line in unclaimed():
			score = similarity(_line_text(line), _po_text(po_line))
			if score > best_score:  # strict > keeps the earliest PO line on ties
				best_index, best_score = po_index, score
		if best_index is not None and best_score >= DESCRIPTION_THRESHOLD:
			assignment[line_index] = (best_index, METHOD_DESCRIPTION)
			claimed.add(best_index)

	results = []
	for line_index, line in enumerate(parsed_lines):
		if line_index in assignment:
			po_index, method = assignment[line_index]
			results.append(_evaluate(line, po_lines[po_index], method, tolerance_pct))
		else:
			results.append(_evaluate(line, None, METHOD_UNMATCHED, tolerance_pct))
	return results


def summarize_match(results, tolerance_pct: float = 2.0) -> tuple[bool, list[str]]:
	"""Reduce match results to ``(all_matched, exceptions)``.

	``all_matched`` is True only when every line found a PO line *and* stayed
	inside tolerance — it is the gate the staging document uses to move from
	*Pending Review* to *Matched*. ``exceptions`` is the human-readable list
	shown on the document and in the queue report.
	"""
	results = list(results)
	exceptions: list[str] = []
	all_matched = bool(results)

	for result in results:
		line = result.parsed_line
		label = f"Line {line.line_id or '?'} ({line.item_name or line.description or 'no description'})"

		if not result.matched:
			all_matched = False
			exceptions.append(f"{label}: no matching Purchase Order line found")
			continue

		po_line = result.po_line
		po_ref = po_line.get("po_name") or "?"

		if abs(result.price_variance_pct) > tolerance_pct:
			all_matched = False
			exceptions.append(
				f"{label}: price {line.unit_price:.2f} vs PO {po_ref} rate "
				f"{float(po_line.get('rate') or 0):.2f} — {result.price_variance_pct:+.2f}% "
				f"(tolerance {tolerance_pct:g}%)"
			)

		if abs(result.qty_variance_pct) > tolerance_pct:
			all_matched = False
			exceptions.append(
				f"{label}: quantity {line.qty:g} vs PO {po_ref} quantity "
				f"{float(po_line.get('qty') or 0):g} — {result.qty_variance_pct:+.2f}% "
				f"(tolerance {tolerance_pct:g}%)"
			)

		remaining = po_line.get("remaining_qty")
		if remaining is not None and float(line.qty or 0) > float(remaining) + 1e-9:
			all_matched = False
			exceptions.append(
				f"{label}: invoiced quantity {line.qty:g} exceeds the quantity still "
				f"open on PO {po_ref} ({float(remaining):g})"
			)

	if not results:
		exceptions.append("The invoice has no lines to match")

	return all_matched, exceptions
