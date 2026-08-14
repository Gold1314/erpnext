# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe adapters for the pure consolidation engine.

Everything database-facing lives here; ``engine.py`` / ``models.py`` stay
frappe-free. Loaders return the pure model types from ``models.py``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, flt, get_last_day, getdate

from erpnext.accounts.consolidation.models import (
	BALANCE_SHEET_ROOT_TYPES,
	PROFIT_AND_LOSS_ROOT_TYPES,
	UNCLOSED_PRIOR_PERIODS_ACCOUNT,
	CompanyBalance,
	EliminationPair,
	OwnershipEdge,
	TranslationRates,
)
from erpnext.setup.utils import get_exchange_rate


def get_company_tree(parent: str) -> list[str]:
	"""All descendant companies of ``parent`` via ``Company.parent_company``
	(breadth-first; ``parent`` itself is not included)."""
	descendants: list[str] = []
	queue = [parent]
	seen = {parent}
	while queue:
		current = queue.pop(0)
		for child in frappe.get_all("Company", filters={"parent_company": current}, pluck="name"):
			if child in seen:
				continue
			seen.add(child)
			descendants.append(child)
			queue.append(child)
	return descendants


def get_balances(companies, from_date, to_date, report_date=None) -> list[CompanyBalance]:
	"""Signed (debit-positive) balances per company per leaf account.

	- Balance-sheet accounts: cumulative balance up to ``report_date``
	  (defaults to ``to_date``).
	- P&L accounts: movement within ``from_date``..``to_date``.

	Because the balance-sheet side is cumulative while the P&L side is a
	period movement, a company's rows generally do not sum to zero (prior
	periods' unclosed profit sits in the gap). A synthetic
	"Unclosed Prior Periods Profit / Loss" Equity line is appended per
	company so each local column balances — the same idea as the
	"Unclosed Fiscal Years Profit / Loss" row in the existing consolidated
	financial statement report. The translate step relies on this.
	"""
	report_date = report_date or to_date
	balances: list[CompanyBalance] = []

	for company in companies:
		company_total = 0.0
		for root_types, start, end in (
			(BALANCE_SHEET_ROOT_TYPES, None, report_date),
			(PROFIT_AND_LOSS_ROOT_TYPES, from_date, to_date),
		):
			for row in _query_balances(company, root_types, start, end):
				amount = flt(row.balance)
				if not amount:
					continue
				balances.append(CompanyBalance(company, row.account, row.root_type, amount))
				company_total += amount

		if abs(company_total) > 0.005:
			balances.append(CompanyBalance(company, UNCLOSED_PRIOR_PERIODS_ACCOUNT, "Equity", -company_total))

	return balances


def _query_balances(company, root_types, start_date, end_date):
	gle = frappe.qb.DocType("GL Entry")
	account = frappe.qb.DocType("Account")

	query = (
		frappe.qb.from_(gle)
		.inner_join(account)
		.on(account.name == gle.account)
		.select(
			gle.account,
			account.root_type,
			Sum(gle.debit - gle.credit).as_("balance"),
		)
		.where(
			(gle.company == company)
			& (gle.is_cancelled == 0)
			& (gle.posting_date <= end_date)
			& (account.root_type.isin(list(root_types)))
		)
		.groupby(gle.account, account.root_type)
	)
	if start_date:
		query = query.where(gle.posting_date >= start_date)

	return query.run(as_dict=True)


def get_rates(
	companies, presentation_currency, report_date, from_date, to_date
) -> tuple[dict[str, TranslationRates], list[str]]:
	"""Closing and average translation rates per company.

	- Closing rate: ``erpnext.setup.utils.get_exchange_rate`` at
	  ``report_date``.
	- Average rate: simple mean of the month-end rates inside
	  ``from_date``..``to_date``. When no month-end rate is available the
	  closing rate is used as a fallback and a warning is returned —
	  documented simplification, not a proper daily-weighted average.

	Returns ``(rates_by_company, warnings)``.
	"""
	rates: dict[str, TranslationRates] = {}
	warnings: list[str] = []

	for company in companies:
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		if not company_currency or company_currency == presentation_currency:
			rates[company] = TranslationRates(1.0, 1.0)
			continue

		closing = flt(get_exchange_rate(company_currency, presentation_currency, report_date))
		if not closing:
			closing = 1.0
			warnings.append(
				_("No closing exchange rate found for {0} -> {1} on {2}; using 1.0.").format(
					company_currency, presentation_currency, report_date
				)
			)

		samples = []
		for month_end in _month_end_dates(from_date, to_date):
			rate = flt(get_exchange_rate(company_currency, presentation_currency, month_end))
			if rate:
				samples.append(rate)

		if samples:
			average = sum(samples) / len(samples)
		else:
			average = closing
			warnings.append(
				_(
					"No month-end exchange rates found for {0} -> {1} between {2} and {3}; "
					"using the closing rate as the average."
				).format(company_currency, presentation_currency, from_date, to_date)
			)

		rates[company] = TranslationRates(closing_rate=closing, average_rate=average)

	return rates, warnings


def _month_end_dates(from_date, to_date):
	"""Month-end dates within the period; falls back to ``to_date`` when the
	period contains no month end."""
	from_date, to_date = getdate(from_date), getdate(to_date)
	dates = []
	current = get_last_day(from_date)
	while current <= to_date:
		dates.append(current)
		current = get_last_day(add_months(current, 1))
	return dates or [to_date]


def get_ownership_edges(parent: str, as_of=None) -> list[OwnershipEdge]:
	"""All Consolidation Ownership records effective at ``as_of`` (or all,
	when ``as_of`` is not given). The engine walks them from ``parent``;
	edges outside the group are simply never visited."""
	records = frappe.get_all(
		"Consolidation Ownership",
		fields=[
			"parent_company",
			"subsidiary",
			"ownership_percent",
			"consolidation_method",
			"effective_from",
			"effective_to",
		],
	)

	edges = []
	as_of = getdate(as_of) if as_of else None
	for record in records:
		if as_of:
			if record.effective_from and getdate(record.effective_from) > as_of:
				continue
			if record.effective_to and getdate(record.effective_to) < as_of:
				continue
		edges.append(
			OwnershipEdge(
				parent=record.parent_company,
				subsidiary=record.subsidiary,
				percent=flt(record.ownership_percent),
				method=record.consolidation_method or "Full",
			)
		)
	return edges


def get_elimination_pairs(parent: str) -> list[EliminationPair]:
	"""Account pairs of all enabled Elimination Rules of ``parent``."""
	rules = frappe.get_all(
		"Elimination Rule",
		filters={"parent_company": parent, "enabled": 1},
		pluck="name",
	)
	if not rules:
		return []

	rows = frappe.get_all(
		"Elimination Rule Account",
		filters={"parent": ("in", rules), "parenttype": "Elimination Rule"},
		fields=["parent", "company_a", "account_a", "company_b", "account_b"],
		order_by="parent, idx",
	)
	return [
		EliminationPair(
			company_a=row.company_a,
			account_a=row.account_a,
			company_b=row.company_b,
			account_b=row.account_b,
			rule=row.parent,
		)
		for row in rows
	]


def get_company_abbrs(companies) -> list[str]:
	return [
		abbr
		for abbr in (frappe.get_cached_value("Company", company, "abbr") for company in companies)
		if abbr
	]


@frappe.whitelist()
def suggest_intercompany_pairs(parent: str) -> list[dict]:
	"""Suggest candidate elimination pairs for the group under ``parent``.

	Sources, in order:

	1. Internal customers (``Customer.is_internal_customer`` /
	   ``represents_company``): the selling company books a receivable, the
	   represented company a payable.
	2. Internal suppliers (``Supplier.is_internal_supplier`` /
	   ``represents_company``): mirror image.
	3. Party Links (Customer <-> Supplier of the same economic party) are
	   implied by 1 + 2 — internal parties are created with a Party Link —
	   so no separate handling is needed.

	The receivable/payable accounts suggested are the companies' default
	receivable/payable accounts. This returns *suggestions only*; it never
	creates Elimination Rules.
	"""
	frappe.has_permission("Elimination Rule", "read", throw=True)

	group_companies = [parent, *get_company_tree(parent)]
	group_set = set(group_companies)

	defaults = {}
	for company in group_companies:
		receivable, payable = frappe.get_cached_value(
			"Company", company, ["default_receivable_account", "default_payable_account"]
		)
		defaults[company] = {"receivable": receivable, "payable": payable}

	suggestions = []
	seen = set()

	def add_suggestion(seller, buyer, source):
		"""seller books the receivable, buyer the payable."""
		if seller == buyer or seller not in group_set or buyer not in group_set:
			return
		key = (seller, buyer)
		if key in seen:
			return
		seen.add(key)
		suggestions.append(
			{
				"company_a": seller,
				"account_a": defaults[seller]["receivable"],
				"company_b": buyer,
				"account_b": defaults[buyer]["payable"],
				"source": source,
				"complete": bool(defaults[seller]["receivable"] and defaults[buyer]["payable"]),
			}
		)

	for customer in frappe.get_all(
		"Customer",
		filters={"is_internal_customer": 1, "represents_company": ("in", group_companies)},
		fields=["name", "represents_company"],
	):
		allowed = frappe.get_all(
			"Allowed To Transact With",
			filters={"parenttype": "Customer", "parent": customer.name},
			pluck="company",
		)
		for seller in allowed or group_companies:
			add_suggestion(seller, customer.represents_company, f"Internal Customer {customer.name}")

	for supplier in frappe.get_all(
		"Supplier",
		filters={"is_internal_supplier": 1, "represents_company": ("in", group_companies)},
		fields=["name", "represents_company"],
	):
		allowed = frappe.get_all(
			"Allowed To Transact With",
			filters={"parenttype": "Supplier", "parent": supplier.name},
			pluck="company",
		)
		for buyer in allowed or group_companies:
			add_suggestion(supplier.represents_company, buyer, f"Internal Supplier {supplier.name}")

	return suggestions
