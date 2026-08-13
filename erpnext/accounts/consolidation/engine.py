# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure consolidation engine — no frappe imports.

Pipeline (see :func:`consolidate`):

1. :func:`resolve_group` — walk ownership edges into effective group percents.
2. :func:`translate` — into presentation currency (closing rate for balance
   sheet, average rate for P&L) with a Currency Translation Adjustment plug
   so every translated column still sums to zero.
3. :func:`eliminate` — offset intercompany account pairs by the *smaller*
   magnitude on each side; residuals become exceptions, never silent plugs.
4. :func:`minority_interest` — outside share of each partly-owned Full-method
   subsidiary's equity plus current net income.
5. Merge account keys across companies (strip company abbr suffixes) into one
   consolidated column.

Everything is unit-tested standalone in ``test_consolidation.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import (
	BALANCE_SHEET_ROOT_TYPES,
	CTA_ACCOUNT,
	METHOD_EQUITY,
	METHOD_FULL,
	MI_RECLASS_ACCOUNT,
	MINORITY_INTEREST_ACCOUNT,
	PROFIT_AND_LOSS_ROOT_TYPES,
	CompanyBalance,
	ConsolidationResult,
	EliminationLine,
	EliminationPair,
	GroupMember,
	OwnershipEdge,
	TranslationRates,
	strip_company_abbr,
)

#: below this absolute amount a balance is treated as zero
DEFAULT_TOLERANCE = 0.01


def resolve_group(parent: str, edges: Iterable[OwnershipEdge]) -> dict[str, GroupMember]:
	"""Resolve the transitive consolidation group under ``parent``.

	Effective percents multiply down the chain (parent owns 80% of A, A owns
	60% of B -> B is 48% effective) and *sum* across parallel ownership paths
	(capped at 100). Descent stops below an Equity-method edge — an
	equity-accounted investee is not consolidated line by line, so its own
	subsidiaries do not enter the group either.

	Raises ``ValueError`` on an ownership cycle.
	"""
	children: dict[str, list[OwnershipEdge]] = {}
	for edge in edges:
		children.setdefault(edge.parent, []).append(edge)

	members: dict[str, GroupMember] = {}

	def visit(node: str, factor: float, path: frozenset) -> None:
		for edge in children.get(node, ()):
			sub = edge.subsidiary
			if sub == node:
				raise ValueError(f"Ownership cycle detected: {sub} owns itself")
			if sub in path:
				raise ValueError(f"Ownership cycle detected involving {sub}")

			effective = factor * edge.percent / 100.0
			if sub in members:
				previous = members[sub]
				method = METHOD_EQUITY if METHOD_EQUITY in (previous.method, edge.method) else METHOD_FULL
				percent = min(previous.percent + effective * 100.0, 100.0)
			else:
				method = edge.method
				percent = effective * 100.0
			members[sub] = GroupMember(sub, percent, method)

			if edge.method == METHOD_FULL:
				visit(sub, effective, path | {sub})

	visit(parent, 1.0, frozenset({parent}))
	return members


def translate(
	balances: Iterable[CompanyBalance],
	rates: Mapping[str, TranslationRates],
	tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[list[CompanyBalance], dict[str, float]]:
	"""Translate balances into the presentation currency.

	Balance-sheet accounts (Asset / Liability / Equity) translate at the
	closing rate, P&L accounts (Income / Expense) at the average rate.
	Because the two rates differ, a balanced local trial balance no longer
	sums to zero after translation; the difference is plugged into a
	synthetic "Currency Translation Adjustment" equity line per company so
	each translated column balances again.

	Precondition: each company's incoming balances sum to (approximately)
	zero — the loaders guarantee this with an "Unclosed Prior Periods
	Profit / Loss" balancing line. Any local imbalance would otherwise be
	absorbed by the CTA plug.

	Returns ``(translated_balances_including_cta, cta_by_company)`` where the
	CTA amounts are signed debit-positive (a credit CTA is negative).
	"""
	translated: list[CompanyBalance] = []
	totals: dict[str, float] = {}

	for bal in balances:
		company_rates = rates.get(bal.company) or TranslationRates()
		rate = (
			company_rates.closing_rate
			if bal.root_type in BALANCE_SHEET_ROOT_TYPES
			else company_rates.average_rate
		)
		amount = bal.balance * rate
		translated.append(CompanyBalance(bal.company, bal.account, bal.root_type, amount))
		totals[bal.company] = totals.get(bal.company, 0.0) + amount

	cta_by_company: dict[str, float] = {}
	for company, total in totals.items():
		plug = -total
		cta_by_company[company] = plug if abs(plug) > tolerance else 0.0
		if abs(plug) > tolerance:
			translated.append(CompanyBalance(company, CTA_ACCOUNT, "Equity", plug))

	return translated, cta_by_company


def eliminate(
	balances: Iterable[CompanyBalance],
	pairs: Iterable[EliminationPair],
	tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[list[CompanyBalance], list[EliminationLine], list[str]]:
	"""Apply intercompany elimination pairs.

	For each pair the two net balances must carry *opposite* signs (one side
	receivable/debit, the other payable/credit). The engine eliminates
	``min(|a|, |b|)`` against **both** sides — each side moves toward zero by
	the same magnitude, so the pair's elimination lines always net to zero
	and can never unbalance the consolidated column. Any residual
	(``a + b != 0``) is reported as an out-of-balance exception showing both
	balances; it is **never** silently plugged.

	Returns ``(balances_after_elimination, elimination_lines, exceptions)``.
	The returned balances are the input plus one adjustment
	``CompanyBalance`` per elimination line (the engine is additive — it
	never mutates its inputs).
	"""
	balances = list(balances)
	net: dict[tuple[str, str], float] = {}
	root_type: dict[tuple[str, str], str] = {}
	for bal in balances:
		key = (bal.company, bal.account)
		net[key] = net.get(key, 0.0) + bal.balance
		root_type.setdefault(key, bal.root_type)

	lines: list[EliminationLine] = []
	exceptions: list[str] = []

	for pair in pairs:
		key_a = (pair.company_a, pair.account_a)
		key_b = (pair.company_b, pair.account_b)
		bal_a = net.get(key_a, 0.0)
		bal_b = net.get(key_b, 0.0)
		label = pair.rule or "Elimination pair"

		if abs(bal_a) <= tolerance and abs(bal_b) <= tolerance:
			continue

		if bal_a * bal_b >= 0:
			# one-sided or same-sign balances: nothing offsets, flag it all
			exceptions.append(
				f"{label}: balances do not offset — "
				f"{pair.account_a} ({pair.company_a}) = {bal_a:.2f} vs "
				f"{pair.account_b} ({pair.company_b}) = {bal_b:.2f}"
			)
			continue

		magnitude = min(abs(bal_a), abs(bal_b))
		adj_a = -magnitude if bal_a > 0 else magnitude
		adj_b = -magnitude if bal_b > 0 else magnitude
		lines.append(EliminationLine(pair.company_a, pair.account_a, adj_a, pair.rule))
		lines.append(EliminationLine(pair.company_b, pair.account_b, adj_b, pair.rule))
		net[key_a] = bal_a + adj_a
		net[key_b] = bal_b + adj_b

		residual = bal_a + bal_b
		if abs(residual) > tolerance:
			exceptions.append(
				f"{label}: out of balance by {residual:.2f} — "
				f"{pair.account_a} ({pair.company_a}) = {bal_a:.2f} vs "
				f"{pair.account_b} ({pair.company_b}) = {bal_b:.2f}; "
				f"eliminated {magnitude:.2f} on both sides, residual left in place"
			)

	adjusted = balances + [
		CompanyBalance(
			line.company,
			line.account,
			root_type.get((line.company, line.account), ""),
			line.amount,
		)
		for line in lines
	]
	return adjusted, lines, exceptions


def minority_interest(
	balances_after_elim: Iterable[CompanyBalance],
	group: Mapping[str, GroupMember],
	tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[dict[str, float], float]:
	"""Minority (non-controlling) interest per partly-owned subsidiary.

	Simplification (documented, deliberate): MI is computed on each
	Full-method subsidiary's post-elimination **equity total plus current
	net income** — there is no split of historical reserves between pre- and
	post-acquisition, no fair-value adjustments and no goodwill allocation.

	Returns ``(mi_by_company, mi_total)`` with amounts **credit-positive**
	(a positive number is the credit balance attributable to outside
	shareholders).
	"""
	equity: dict[str, float] = {}
	profit_and_loss: dict[str, float] = {}
	for bal in balances_after_elim:
		if bal.root_type == "Equity":
			equity[bal.company] = equity.get(bal.company, 0.0) + bal.balance
		elif bal.root_type in PROFIT_AND_LOSS_ROOT_TYPES:
			profit_and_loss[bal.company] = profit_and_loss.get(bal.company, 0.0) + bal.balance

	mi_by_company: dict[str, float] = {}
	for member in group.values():
		if member.method != METHOD_FULL or member.percent >= 100.0:
			continue
		# flip debit-positive to credit-positive
		equity_credit = -equity.get(member.company, 0.0)
		net_income_credit = -profit_and_loss.get(member.company, 0.0)
		outside_share = (100.0 - member.percent) / 100.0
		amount = outside_share * (equity_credit + net_income_credit)
		if abs(amount) > tolerance:
			mi_by_company[member.company] = amount

	return mi_by_company, sum(mi_by_company.values())


def consolidate(
	parent: str,
	balances: Iterable[CompanyBalance],
	edges: Iterable[OwnershipEdge],
	rates: Mapping[str, TranslationRates],
	pairs: Iterable[EliminationPair],
	abbrs: Iterable[str],
	tolerance: float = DEFAULT_TOLERANCE,
) -> ConsolidationResult:
	"""Run the full consolidation pipeline. Pure — all inputs are data.

	``abbrs`` is the list of company abbreviations used to merge account
	names across companies ("Debtors - PA" and "Debtors - SB" both become
	"Debtors").

	The minority-interest split is presented as two offsetting synthetic
	equity lines in the consolidated column ("Minority Interest", credit,
	and "Equity Attributable to Group (NCI Reclass)", debit) so the
	consolidated balance sheet stays balanced while MI is visible.
	"""
	abbrs = list(abbrs)
	warnings: list[str] = []

	members = resolve_group(parent, edges)
	companies = [parent]
	for member in members.values():
		if member.method == METHOD_EQUITY:
			warnings.append(
				f"{member.company} is held under the Equity method and was excluded "
				f"from line-by-line consolidation (equity pickup is a v2 feature)."
			)
		else:
			companies.append(member.company)

	in_scope = set(companies)
	scoped = [bal for bal in balances if bal.company in in_scope]

	translated, cta_by_company = translate(scoped, rates, tolerance)
	adjusted, elimination_lines, exceptions = eliminate(translated, pairs, tolerance)
	mi_by_company, mi_total = minority_interest(adjusted, members, tolerance)

	columns: dict[str, dict[str, float]] = {company: {} for company in companies}
	root_type_by_key: dict[str, str] = {}
	for bal in translated:
		key = strip_company_abbr(bal.account, abbrs)
		columns[bal.company][key] = columns[bal.company].get(key, 0.0) + bal.balance
		if bal.root_type:
			root_type_by_key.setdefault(key, bal.root_type)

	consolidated: dict[str, float] = {}
	for bal in adjusted:
		key = strip_company_abbr(bal.account, abbrs)
		consolidated[key] = consolidated.get(key, 0.0) + bal.balance
		if bal.root_type:
			root_type_by_key.setdefault(key, bal.root_type)

	if abs(mi_total) > tolerance:
		# reclass within equity: net zero, so the balance sheet stays balanced
		consolidated[MINORITY_INTEREST_ACCOUNT] = consolidated.get(MINORITY_INTEREST_ACCOUNT, 0.0) - mi_total
		consolidated[MI_RECLASS_ACCOUNT] = consolidated.get(MI_RECLASS_ACCOUNT, 0.0) + mi_total
		root_type_by_key.setdefault(MINORITY_INTEREST_ACCOUNT, "Equity")
		root_type_by_key.setdefault(MI_RECLASS_ACCOUNT, "Equity")

	return ConsolidationResult(
		parent=parent,
		companies=companies,
		columns=columns,
		elimination_lines=elimination_lines,
		exceptions=exceptions,
		warnings=warnings,
		cta_by_company=cta_by_company,
		minority_interest_by_company=mi_by_company,
		minority_interest_total=mi_total,
		consolidated=consolidated,
		root_type_by_key=root_type_by_key,
	)
