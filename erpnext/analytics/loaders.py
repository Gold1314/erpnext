# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side input loaders for the semantic metric layer.

All DB access lives here; ``models.py`` / ``engine.py`` / ``registry.py`` stay
pure. Each loader returns a plain ``dict`` of the **named inputs** the registry
declares for the given company and period, plus (where relevant) warnings for
anything that could not be sourced. Nothing here raises on missing data: an
unused module yields ``0`` with a warning so the scorecard still renders.

Agreement with existing reports - what each loader mirrors
----------------------------------------------------------
``get_pl_inputs``
    Mirrors the Profit and Loss Statement's period logic:
    ``erpnext/accounts/report/profit_and_loss_statement/profit_and_loss_statement.py:39-59``
    calls ``get_data(..., ignore_closing_entries=True)``, which lands in
    ``erpnext/accounts/report/financial_statements.py:617`` ->
    ``get_accounting_entries`` (``financial_statements.py:687``): GL Entry rows
    with ``is_cancelled = 0``, ``posting_date`` inside the window, on ledger
    accounts (``Account.is_group = 0``, ``financial_statements.py:783``
    ``get_account_filter_query``) of the requested ``root_type``, excluding
    ``voucher_type = 'Period Closing Voucher'``
    (``financial_statements.py:800`` ``apply_additional_conditions``). Income is
    credit-positive, Expense is debit-positive - the same sign convention as the
    statement's "Total Income (Credit)" / "Total Expense (Debit)" rows.
    The COGS bucket uses ``account_type = 'Cost of Goods Sold'``, exactly the
    selection ``erpnext/accounts/report/financial_ratios/financial_ratios.py:108``
    (and ``update_balances``, ``financial_ratios.py:255``) uses for its cogs.

``get_bs_inputs``
    Mirrors ``erpnext.accounts.utils.get_balance_on`` (``utils.py:204``) - it
    literally calls it for the Receivable / Payable / Stock / Cash / Bank
    balances, which is the same call the Financial Ratios report makes in
    ``avg_ratio_balance`` (``financial_ratios.py:264-282``), including the
    ``(opening + closing) / 2`` average used by ``inventory_turns``. Current
    assets/liabilities are summed with the same GL predicate as
    ``get_balance_on`` (``is_cancelled = 0``, ``posting_date <= date``, company
    scoped, ``sum(debit) - sum(credit)`` in company currency).

``get_ar_ageing_inputs``
    Sales Invoice ``outstanding_amount`` vs ``due_date`` - the same two fields
    the Accounts Receivable report ages on.

``get_ops_inputs`` / ``get_governance_inputs``
    Documented in their own docstrings; they read Sales Order/Delivery Note,
    Purchase Order/Purchase Receipt, and the Close Cycle / Anomaly Finding /
    SoD Violation Log / Sales Forecast doctypes built earlier on this branch.
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Max, Sum
from frappe.utils import add_days, flt, getdate, today

from erpnext.accounts.utils import get_balance_on

# ---------------------------------------------------------------------------
# Account classification
# ---------------------------------------------------------------------------
#: Account.account_category values that make up "current assets" - matches the
#: Standard Balance Sheet (IFRS) template shipped in
#: erpnext/accounts/financial_report_template/account_categories.json
CURRENT_ASSET_CATEGORIES = (
	"Cash and Cash Equivalents",
	"Trade Receivables",
	"Other Receivables",
	"Short-term Investments",
	"Stock Assets",
	"Other Current Assets",
)

CURRENT_LIABILITY_CATEGORIES = (
	"Trade Payables",
	"Other Payables",
	"Short-term Borrowings",
	"Short-term Provisions",
	"Current Tax Liabilities",
	"Other Current Liabilities",
)

#: expense account types excluded from "operating expense" (EBIT-style)
NON_OPERATING_EXPENSE_TYPES = ("Depreciation", "Tax")

#: quick assets, as defined by financial_ratios.py::update_balances
QUICK_ASSET_TYPES = ("Bank", "Cash", "Receivable")


def prior_period(period_start, period_end) -> tuple:
	"""The immediately preceding window of equal length (both inclusive)."""
	start, end = getdate(period_start), getdate(period_end)
	length_days = (end - start).days
	prior_end = add_days(start, -1)
	return add_days(prior_end, -length_days), prior_end


# ---------------------------------------------------------------------------
# Profit and loss
# ---------------------------------------------------------------------------
def get_pl_inputs(company: str, period_start, period_end) -> dict:
	"""revenue / cogs / opex / operating_expense for the period.

	Sign convention and row selection mirror the Profit and Loss Statement
	(see module docstring). Returns company-currency amounts.
	"""
	income = _pl_amount(company, period_start, period_end, root_type="Income")
	expense = _pl_amount(company, period_start, period_end, root_type="Expense")
	cogs = _pl_amount(
		company, period_start, period_end, root_type="Expense", account_types=["Cost of Goods Sold"]
	)
	non_operating = _pl_amount(
		company,
		period_start,
		period_end,
		root_type="Expense",
		account_types=list(NON_OPERATING_EXPENSE_TYPES),
	)

	opex = expense - cogs
	return {
		"revenue": income,
		"cogs": cogs,
		"opex": opex,
		"operating_expense": opex - non_operating,
	}


def _pl_amount(company, from_date, to_date, root_type, account_types=None) -> float:
	"""Net movement on ``root_type`` accounts for the window, in company currency.

	Income is returned credit-positive, everything else debit-positive - the
	same orientation as the P&L statement's total rows.
	"""
	gle = frappe.qb.DocType("GL Entry")
	account = frappe.qb.DocType("Account")

	query = (
		frappe.qb.from_(gle)
		.inner_join(account)
		.on(account.name == gle.account)
		.select(
			Sum(gle.debit).as_("debit"),
			Sum(gle.credit).as_("credit"),
		)
		.where(gle.company == company)
		.where(gle.is_cancelled == 0)
		.where(gle.posting_date >= getdate(from_date))
		.where(gle.posting_date <= getdate(to_date))
		# mirrors financial_statements.apply_additional_conditions(ignore_closing_entries=True)
		.where(gle.voucher_type != "Period Closing Voucher")
		.where(account.is_group == 0)
		.where(account.root_type == root_type)
	)

	if account_types:
		query = query.where(account.account_type.isin(account_types))

	row = query.run(as_dict=True)
	debit = flt(row[0].debit) if row else 0.0
	credit = flt(row[0].credit) if row else 0.0

	return credit - debit if root_type in ("Income", "Liability", "Equity") else debit - credit


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------
def get_bs_inputs(company: str, period_start, period_end) -> tuple[dict, list[str]]:
	"""Point-in-time balances as of ``period_end`` (+ two opening balances).

	Receivable / Payable / Stock / Cash / Bank come straight from
	``erpnext.accounts.utils.get_balance_on`` so the numbers are identical to
	the Financial Ratios report's turnover inputs. Liability-side balances are
	sign-flipped to be reported positive.
	"""
	warnings: list[str] = []
	as_of = getdate(period_end)
	opening_date = add_days(getdate(period_start), -1)

	ar_balance = _balance_by_account_type(company, as_of, "Receivable", warnings)
	ap_balance = -1 * _balance_by_account_type(company, as_of, "Payable", warnings)
	inventory_balance = _balance_by_account_type(company, as_of, "Stock", warnings)
	opening_inventory = _balance_by_account_type(company, opening_date, "Stock", warnings)
	opening_ar_balance = _balance_by_account_type(company, opening_date, "Receivable", warnings)

	cash_and_bank = sum(
		_balance_by_account_type(company, as_of, account_type, warnings) for account_type in ("Cash", "Bank")
	)
	# financial_ratios.py::update_balances defines quick assets as Bank + Cash + Receivable
	quick_assets = cash_and_bank + ar_balance

	current_assets, ca_warning = _classified_balance(company, as_of, "Asset")
	current_liabilities, cl_warning = _classified_balance(company, as_of, "Liability")
	warnings.extend(w for w in (ca_warning, cl_warning) if w)

	return {
		"current_assets": current_assets,
		"current_liabilities": current_liabilities,
		"quick_assets": quick_assets,
		"cash_and_bank": cash_and_bank,
		"ar_balance": ar_balance,
		"ap_balance": ap_balance,
		"inventory_balance": inventory_balance,
		# (opening + closing) / 2, exactly as financial_ratios.avg_ratio_balance
		"average_inventory": (opening_inventory + inventory_balance) / 2.0,
		"opening_ar_balance": opening_ar_balance,
	}, warnings


def _balance_by_account_type(company, as_of, account_type, warnings: list[str]) -> float:
	"""``get_balance_on`` for one account type, guarded against an empty CoA.

	``get_balance_on`` builds an ``account in (...)`` clause from the matching
	ledger accounts, which is invalid SQL when nothing matches - so check first.
	"""
	if not frappe.db.exists("Account", {"company": company, "account_type": account_type, "is_group": 0}):
		warnings.append(f"No '{account_type}' ledger account in {company} - treated as 0")
		return 0.0

	return flt(
		get_balance_on(
			date=as_of,
			company=company,
			account_type=account_type,
			in_account_currency=False,
			ignore_account_permission=True,
		)
	)


def _classified_balance(company, as_of, root_type) -> tuple[float, str | None]:
	"""Current asset / current liability balance as of ``as_of``.

	Classification, in order of preference:

	1. accounts tagged ``account_type = 'Current Asset' / 'Current Liability'``
	   (what ``financial_ratios.py::update_balances`` reads) - the whole subtree
	   of any such account is included;
	2. otherwise accounts whose ``account_category`` is one of the current-asset
	   / current-liability categories shipped with the IFRS balance sheet
	   template;
	3. otherwise 0 with a warning asking for the CoA to be tagged.

	Liability balances are returned positive (sign flipped from debit - credit).
	"""
	is_asset = root_type == "Asset"
	account_type = "Current Asset" if is_asset else "Current Liability"
	categories = CURRENT_ASSET_CATEGORIES if is_asset else CURRENT_LIABILITY_CATEGORIES
	sign = 1.0 if is_asset else -1.0

	tagged = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": account_type},
		fields=["name", "lft", "rgt"],
	)
	if tagged:
		accounts = _ledger_accounts_in_subtrees(company, tagged)
		return sign * _balance_for_accounts(company, as_of, accounts), None

	categorised = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"is_group": 0,
			"root_type": root_type,
			"account_category": ("in", categories),
		},
		pluck="name",
	)
	if categorised:
		return sign * _balance_for_accounts(company, as_of, categorised), None

	return 0.0, (
		f"No account in {company} is tagged as '{account_type}' or carries a current "
		f"{root_type.lower()} account category - liquidity metrics treated as 0"
	)


def _ledger_accounts_in_subtrees(company, group_rows) -> list[str]:
	"""Ledger (non-group) accounts inside any of the given lft/rgt ranges."""
	account = frappe.qb.DocType("Account")
	query = (
		frappe.qb.from_(account)
		.select(account.name)
		.where(account.company == company)
		.where(account.is_group == 0)
	)

	condition = None
	for row in group_rows:
		clause = (account.lft >= row.lft) & (account.rgt <= row.rgt)
		condition = clause if condition is None else (condition | clause)

	if condition is not None:
		query = query.where(condition)

	return [row.name for row in query.run(as_dict=True)]


def _balance_for_accounts(company, as_of, accounts) -> float:
	"""``sum(debit) - sum(credit)`` up to ``as_of`` - get_balance_on's predicate."""
	if not accounts:
		return 0.0

	gle = frappe.qb.DocType("GL Entry")
	row = (
		frappe.qb.from_(gle)
		.select(
			Sum(gle.debit).as_("debit"),
			Sum(gle.credit).as_("credit"),
		)
		.where(gle.company == company)
		.where(gle.is_cancelled == 0)
		.where(gle.posting_date <= getdate(as_of))
		.where(gle.account.isin(accounts))
		.run(as_dict=True)
	)

	return flt(row[0].debit) - flt(row[0].credit) if row else 0.0


# ---------------------------------------------------------------------------
# Receivable ageing
# ---------------------------------------------------------------------------
def get_ar_ageing_inputs(company: str, period_start, period_end) -> tuple[dict, list[str]]:
	"""Open receivables split by overdue vs current, as of ``period_end``.

	Reads submitted Sales Invoices with a non-zero ``outstanding_amount``
	posted on or before the period end; overdue means ``due_date`` is before
	the period end. ``outstanding_amount`` is in the party account currency, so
	it is multiplied by the invoice ``conversion_rate`` to land in company
	currency.

	``outstanding_amount`` is a *live* field: for a period end in the past the
	split reflects today's collection state, and the loader says so.
	"""
	warnings: list[str] = []
	as_of = getdate(period_end)
	if as_of < getdate(today()):
		warnings.append(
			"Receivable ageing uses the current Sales Invoice outstanding amounts, "
			"so a historical period end reflects today's collection state"
		)

	si = frappe.qb.DocType("Sales Invoice")
	rows = (
		frappe.qb.from_(si)
		.select(si.outstanding_amount, si.conversion_rate, si.due_date)
		.where(si.docstatus == 1)
		.where(si.company == company)
		.where(si.posting_date <= as_of)
		.where(si.outstanding_amount > 0)
		.run(as_dict=True)
	)

	total = 0.0
	overdue = 0.0
	for row in rows:
		amount = flt(row.outstanding_amount) * (flt(row.conversion_rate) or 1.0)
		total += amount
		if row.due_date and getdate(row.due_date) < as_of:
			overdue += amount

	return {
		"ar_total_outstanding": total,
		"ar_overdue_outstanding": overdue,
		"ar_current_outstanding": total - overdue,
	}, warnings


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def get_ops_inputs(company: str, period_start, period_end) -> tuple[dict, list[str]]:
	"""OTIF and purchase price variance for the period.

	**OTIF (line level).** Population: every Sales Order line that received a
	delivery in the window (submitted, non-return Delivery Note Items carrying
	``so_detail``). A line counts as on-time-in-full when

	- *on time*: the last Delivery Note posting date for that line is on or
	  before the line's promised ``Sales Order Item.delivery_date``, and
	- *in full*: ``Sales Order Item.delivered_qty >= qty`` (a hundredth of a
	  unit of tolerance for float noise).

	Lines with no promised date are counted as on time (nothing was promised).

	**PPV (receipt-time price variance).** For submitted, non-return Purchase
	Receipt lines in the window that are linked to a Purchase Order line, the
	variance is ``qty * (receipt base_rate - order base_rate)`` and the baseline
	is ``qty * order base_rate``; both in company currency. Positive variance =
	paying more than ordered.
	"""
	warnings: list[str] = []
	from_date, to_date = getdate(period_start), getdate(period_end)

	dn = frappe.qb.DocType("Delivery Note")
	dni = frappe.qb.DocType("Delivery Note Item")
	soi = frappe.qb.DocType("Sales Order Item")
	delivered = (
		frappe.qb.from_(dni)
		.inner_join(dn)
		.on(dn.name == dni.parent)
		.inner_join(soi)
		.on(soi.name == dni.so_detail)
		.select(
			soi.name.as_("so_item"),
			soi.delivery_date,
			soi.qty,
			soi.delivered_qty,
			Max(dn.posting_date).as_("last_delivery_date"),
		)
		.where(dn.docstatus == 1)
		.where(dn.company == company)
		.where(dn.is_return == 0)
		.where(dn.posting_date >= from_date)
		.where(dn.posting_date <= to_date)
		.groupby(soi.name, soi.delivery_date, soi.qty, soi.delivered_qty)
		.run(as_dict=True)
	)

	otif_lines = 0
	for row in delivered:
		on_time = not row.delivery_date or getdate(row.last_delivery_date) <= getdate(row.delivery_date)
		in_full = flt(row.delivered_qty) >= flt(row.qty) - 0.01
		if on_time and in_full:
			otif_lines += 1

	if not delivered:
		warnings.append("No sales order lines were delivered in the period - OTIF unavailable")

	pr = frappe.qb.DocType("Purchase Receipt")
	pri = frappe.qb.DocType("Purchase Receipt Item")
	poi = frappe.qb.DocType("Purchase Order Item")
	ppv = (
		frappe.qb.from_(pri)
		.inner_join(pr)
		.on(pr.name == pri.parent)
		.inner_join(poi)
		.on(poi.name == pri.purchase_order_item)
		.select(
			Sum(pri.qty * (pri.base_rate - poi.base_rate)).as_("variance"),
			Sum(pri.qty * poi.base_rate).as_("baseline"),
		)
		.where(pr.docstatus == 1)
		.where(pr.company == company)
		.where(pr.is_return == 0)
		.where(pr.posting_date >= from_date)
		.where(pr.posting_date <= to_date)
		.run(as_dict=True)
	)

	variance = flt(ppv[0].variance) if ppv else 0.0
	baseline = flt(ppv[0].baseline) if ppv else 0.0
	if not baseline:
		warnings.append("No purchase receipts linked to a purchase order in the period - PPV unavailable")

	return {
		"otif_lines": float(otif_lines),
		"delivered_lines": float(len(delivered)),
		"ppv_variance_amount": variance,
		"ppv_baseline_amount": baseline,
	}, warnings


# ---------------------------------------------------------------------------
# Governance / close
# ---------------------------------------------------------------------------
def get_governance_inputs(company: str, period_start, period_end) -> tuple[dict, list[str]]:
	"""Close duration, open anomalies, open SoD violations, forecast MAPE.

	- **Close duration**: Completed ``Close Cycle`` records whose
	  ``period_end_date`` falls in the window; the duration of one cycle is the
	  days from ``period_end_date`` to the latest ``Close Task.signed_off_on``
	  of that cycle. Cycles with no signed-off task are skipped (nothing to
	  measure) and reported as a warning.
	- **Open high anomalies**: ``Anomaly Finding`` rows for the company with
	  ``severity = 'High'`` and status in Open/Investigating.
	- **Open SoD violations**: ``SoD Violation Log`` rows with
	  ``status = 'Open'``. That doctype is user-scoped, not company-scoped, so
	  the number is site-wide (warned).
	- **Forecast MAPE**: the most recent submitted ``Sales Forecast`` for the
	  company at or before the period end; the average of its
	  ``Sales Forecast Item.mape`` values (rows without a stored mape are
	  ignored).
	"""
	warnings: list[str] = []
	from_date, to_date = getdate(period_start), getdate(period_end)

	inputs = {
		"close_cycle_days_total": 0.0,
		"close_cycles_completed": 0.0,
		"open_high_anomalies": 0.0,
		"open_sod_violations": 0.0,
		"forecast_mape_sum": 0.0,
		"forecast_mape_count": 0.0,
	}

	# --- close duration --------------------------------------------------
	if frappe.db.table_exists("Close Cycle"):
		cycles = frappe.get_all(
			"Close Cycle",
			filters={
				"company": company,
				"status": "Completed",
				"period_end_date": ("between", [from_date, to_date]),
			},
			fields=["name", "period_end_date"],
		)
		measured = 0
		total_days = 0.0
		for cycle in cycles:
			last_sign_off = frappe.get_all(
				"Close Task",
				filters={"close_cycle": cycle.name, "signed_off_on": ("is", "set")},
				fields=["signed_off_on"],
				order_by="signed_off_on desc",
				limit=1,
			)
			if not last_sign_off:
				continue
			total_days += (getdate(last_sign_off[0].signed_off_on) - getdate(cycle.period_end_date)).days
			measured += 1

		inputs["close_cycle_days_total"] = float(total_days)
		inputs["close_cycles_completed"] = float(measured)
		if cycles and not measured:
			warnings.append("Completed close cycles have no signed-off tasks - close duration unavailable")
		elif not cycles:
			warnings.append("No completed Close Cycle ends in the period - close duration unavailable")
	else:
		warnings.append("Close Cycle is not installed - close duration unavailable")

	# --- open high-severity anomalies -----------------------------------
	if frappe.db.table_exists("Anomaly Finding"):
		inputs["open_high_anomalies"] = float(
			frappe.db.count(
				"Anomaly Finding",
				{
					"company": company,
					"severity": "High",
					"status": ("in", ["Open", "Investigating"]),
				},
			)
		)
	else:
		warnings.append("Anomaly Finding is not installed - open anomalies treated as 0")

	# --- open SoD violations ---------------------------------------------
	if frappe.db.table_exists("SoD Violation Log"):
		inputs["open_sod_violations"] = float(frappe.db.count("SoD Violation Log", {"status": "Open"}))
		if inputs["open_sod_violations"]:
			warnings.append("SoD violations are user-scoped, not company-scoped - the count is site-wide")
	else:
		warnings.append("SoD Violation Log is not installed - open violations treated as 0")

	# --- forecast accuracy ------------------------------------------------
	if frappe.db.table_exists("Sales Forecast"):
		forecast = frappe.get_all(
			"Sales Forecast",
			filters={"company": company, "docstatus": 1, "posting_date": ("<=", to_date)},
			fields=["name"],
			order_by="posting_date desc, creation desc",
			limit=1,
		)
		if forecast:
			rows = frappe.get_all(
				"Sales Forecast Item",
				filters={"parent": forecast[0].name, "parenttype": "Sales Forecast"},
				fields=["mape"],
			)
			scored = [flt(row.mape) for row in rows if row.mape is not None]
			inputs["forecast_mape_sum"] = float(sum(scored))
			inputs["forecast_mape_count"] = float(len(scored))
			if not scored:
				warnings.append(
					f"Sales Forecast {forecast[0].name} has no stored MAPE - forecast accuracy unavailable"
				)
		else:
			warnings.append("No submitted Sales Forecast for the company - forecast accuracy unavailable")
	else:
		warnings.append("Sales Forecast is not installed - forecast accuracy unavailable")

	return inputs, warnings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def collect_inputs(company: str, period_start, period_end) -> tuple[dict, list[str]]:
	"""Every named input the KPI pack declares, for one company and period.

	Returns ``(inputs, warnings)``. Individual loader failures are caught and
	converted into warnings so one cold feed can never blank the board; the
	inputs a failed loader would have supplied are simply absent, and the
	engine reports them as unavailable per metric.
	"""
	inputs: dict = {}
	warnings: list[str] = []

	def run(label, fn):
		try:
			result = fn()
		except Exception:
			frappe.log_error(title=f"KPI loader failed: {label}", message=frappe.get_traceback())
			warnings.append(f"{label} could not be loaded - see the error log")
			return
		if isinstance(result, tuple):
			values, loader_warnings = result
			warnings.extend(loader_warnings)
		else:
			values = result
		inputs.update(values)

	run("Profit and loss", lambda: get_pl_inputs(company, period_start, period_end))
	run("Balance sheet", lambda: get_bs_inputs(company, period_start, period_end))
	run("Receivable ageing", lambda: get_ar_ageing_inputs(company, period_start, period_end))
	run("Operations", lambda: get_ops_inputs(company, period_start, period_end))
	run("Governance", lambda: get_governance_inputs(company, period_start, period_end))

	prior_start, prior_end = prior_period(period_start, period_end)
	run(
		"Prior period revenue",
		lambda: {"prior_revenue": get_pl_inputs(company, prior_start, prior_end).get("revenue", 0.0)},
	)

	return inputs, warnings
