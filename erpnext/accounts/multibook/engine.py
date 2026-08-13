# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure engine that turns accounting policy rules + account movements into
balanced, book-specific Journal Entry lines.

No frappe imports - unit-tested stand-alone by ``test_multibook.py``. All DB
access lives in ``loaders.py``; the ``Finance Book Adjustment`` doctype is the
posting adapter.

Money handling: every emitted line amount is rounded **half-up to 2 decimals
at line level** using :class:`decimal.Decimal` (via ``Decimal(str(x))`` so
binary-float noise like ``12.344999...`` does not flip the rounding).
Consolidation sums Decimals, so the result is exact at 2dp. A defensive
balance assertion (:func:`balance_lines`) still runs on the final set: a
residual within ``tolerance`` (default one cent) is absorbed into the largest
line; anything larger raises :class:`ValueError`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

from erpnext.accounts.multibook.models import (
	EXCLUDE,
	MANUAL_AMOUNT,
	RECLASSIFY,
	RULE_TYPES,
	AdjustmentLine,
	PolicyRule,
)

TWO_PLACES = Decimal("0.01")


def round2(value: float | Decimal) -> Decimal:
	"""Round half-up to 2 decimals (line-level rounding rule)."""
	if not isinstance(value, Decimal):
		value = Decimal(str(value))
	return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def validate_rule(rule: PolicyRule) -> None:
	"""Raise ``ValueError`` when a rule is not executable.

	Mirrors (and backs) the ``Accounting Policy Rule.validate`` controller so
	the engine is safe even when called with hand-built rules.
	"""
	if rule.rule_type not in RULE_TYPES:
		raise ValueError(f"Rule {rule.name}: unknown rule type {rule.rule_type!r}")

	if rule.rule_type in (RECLASSIFY, EXCLUDE):
		if not rule.source_account or not rule.target_account:
			raise ValueError(
				f"Rule {rule.name}: {rule.rule_type} needs both a source account and a target account"
			)
		if rule.source_account == rule.target_account:
			raise ValueError(f"Rule {rule.name}: source account and target account must differ")
		if not (0 < float(rule.percentage) <= 100):
			raise ValueError(
				f"Rule {rule.name}: percentage must be greater than 0 and at most 100, got {rule.percentage}"
			)

	if rule.rule_type == MANUAL_AMOUNT:
		if not rule.manual_debit_account or not rule.manual_credit_account:
			raise ValueError(f"Rule {rule.name}: Manual Amount needs both a debit and a credit account")
		if rule.manual_debit_account == rule.manual_credit_account:
			raise ValueError(f"Rule {rule.name}: manual debit and credit accounts must differ")
		if round2(rule.manual_amount) <= 0:
			raise ValueError(f"Rule {rule.name}: manual amount must be a positive amount")


def _reversal_lines(rule: PolicyRule, net_movement: float) -> list[tuple[str, Decimal, Decimal]]:
	"""Shared mechanics of Reclassify and Exclude.

	Both emit the entry that reverses ``percentage``% of the source account's
	net movement inside the book, offset to the target account:

	- net-debit movement ``M`` (M > 0): **Credit source, Debit target** by
	  ``round2(M x pct)``;
	- net-credit movement (M < 0): mirrored - **Debit source, Credit target**;
	- zero movement: no lines.

	The two rule types differ only in intent (Reclassify's target is another
	P&L/BS account; Exclude's target is the offset that keeps the JE balanced,
	e.g. a "GAAP adjustment" equity account).
	"""
	amount = round2(abs(Decimal(str(net_movement))) * Decimal(str(rule.percentage)) / Decimal("100"))
	if amount == 0:
		return []
	if net_movement > 0:
		return [(rule.source_account, Decimal("0"), amount), (rule.target_account, amount, Decimal("0"))]
	return [(rule.source_account, amount, Decimal("0")), (rule.target_account, Decimal("0"), amount)]


def _rule_lines(rule: PolicyRule, movements: Mapping[str, float]) -> list[tuple[str, Decimal, Decimal]]:
	"""Raw (account, debit, credit) Decimal triples for one rule."""
	if rule.rule_type in (RECLASSIFY, EXCLUDE):
		return _reversal_lines(rule, float(movements.get(rule.source_account) or 0))

	# MANUAL_AMOUNT
	amount = round2(rule.manual_amount)
	return [
		(rule.manual_debit_account, amount, Decimal("0")),
		(rule.manual_credit_account, Decimal("0"), amount),
	]


def build_adjustment_lines(
	rules: Iterable[PolicyRule], movements: Mapping[str, float]
) -> list[AdjustmentLine]:
	"""Compute the consolidated, balanced adjustment lines for one book/period.

	``movements`` maps account -> net movement (``SUM(debit) - SUM(credit)``,
	positive = net debit) over the adjustment period, taken from the *common*
	(untagged) GL layer only - see ``loaders.get_account_movements``.

	Consolidation: duplicate accounts across rules are summed and **netted**
	(debit total minus credit total keeps only the larger side), zero lines
	are dropped, and ``source_rules`` accumulates every contributing rule
	name. Line order follows first appearance of each account.

	Raises ``ValueError`` for invalid rules or an unbalanced result (see
	:func:`balance_lines`).
	"""
	totals: dict[str, list] = {}  # account -> [debit Decimal, credit Decimal, [rule names]]
	order: list[str] = []

	for rule in rules:
		validate_rule(rule)
		for account, debit, credit in _rule_lines(rule, movements):
			if account not in totals:
				totals[account] = [Decimal("0"), Decimal("0"), []]
				order.append(account)
			totals[account][0] += debit
			totals[account][1] += credit
			if rule.name not in totals[account][2]:
				totals[account][2].append(rule.name)

	lines: list[AdjustmentLine] = []
	for account in order:
		debit, credit, rule_names = totals[account]
		net = round2(debit - credit)
		if net == 0:
			continue
		lines.append(
			AdjustmentLine(
				account=account,
				debit=float(net) if net > 0 else 0.0,
				credit=float(-net) if net < 0 else 0.0,
				source_rules=tuple(rule_names),
			)
		)

	return balance_lines(lines)


def balance_lines(lines: list[AdjustmentLine], tolerance: float = 0.01) -> list[AdjustmentLine]:
	"""Assert sum(debit) == sum(credit) at 2dp; absorb a cent-level residual.

	A residual with ``abs(residual) <= tolerance`` (rounding noise) is pushed
	into the **largest line** (largest debit-or-credit amount): a debit line
	absorbs ``residual`` by shrinking, a credit line by growing (and vice
	versa for a negative residual). Anything beyond tolerance - or an
	absorption that would drive a line negative - raises ``ValueError``.
	"""
	if not lines:
		return lines

	residual = round2(sum(Decimal(str(line.debit)) for line in lines)) - round2(
		sum(Decimal(str(line.credit)) for line in lines)
	)
	if residual == 0:
		return lines

	if abs(residual) > round2(tolerance):
		raise ValueError(
			f"Adjustment lines are unbalanced: total debit exceeds total credit by {residual} "
			"(beyond rounding tolerance)"
		)

	largest = max(lines, key=lambda line: max(line.debit, line.credit))
	if largest.debit:
		new_amount = round2(Decimal(str(largest.debit)) - residual)
		if new_amount < 0:
			raise ValueError("Cannot absorb rounding residual: largest line would turn negative")
		largest.debit = float(new_amount)
	else:
		new_amount = round2(Decimal(str(largest.credit)) + residual)
		if new_amount < 0:
			raise ValueError("Cannot absorb rounding residual: largest line would turn negative")
		largest.credit = float(new_amount)

	return [line for line in lines if line.debit or line.credit]


def periods_overlap(from_1, to_1, from_2, to_2) -> bool:
	"""True when [from_1, to_1] and [from_2, to_2] share at least one day.

	Inclusive on both ends (touching endpoints overlap). Works on any
	consistently comparable values - ``datetime.date`` or ISO date strings.
	Used by ``Finance Book Adjustment`` to reject a second submitted
	adjustment for the same company + finance book over an overlapping period
	(re-run / compounding protection, see DESIGN.md).
	"""
	return from_1 <= to_2 and from_2 <= to_1
