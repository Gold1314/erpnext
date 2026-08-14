# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The shipped KPI pack - the governed catalog itself.

Every metric is declared once, here, as **data**: label, unit, grain,
direction, category, human formula text, the named inputs it needs and the
pure arithmetic that turns those inputs into a number. There is no SQL in
this module - ``loaders.py`` is solely responsible for producing the named
inputs, which keeps the definitions reviewable by finance rather than by
engineering.

Where ``erpnext/accounts/report/financial_ratios`` already computes an
equivalent number, the formula here is **mirrored exactly** so the two can
never disagree; each such metric says so in its ``description`` and the
mapping is tabulated in ``DESIGN.md``.

No frappe imports: this module is importable (and testable) without a site.
"""

from __future__ import annotations

from erpnext.analytics.models import (
	COUNT,
	CURRENCY,
	DAYS,
	GOVERNANCE,
	GRAIN_COMPANY,
	GRAIN_COMPANY_PERIOD,
	GROWTH,
	HIGHER_IS_BETTER,
	LIQUIDITY,
	LOWER_IS_BETTER,
	NEUTRAL,
	OPERATIONS,
	PERCENT,
	PROFITABILITY,
	RATIO,
	RECEIVABLES,
	MetricSpec,
	Threshold,
)

#: days in a year used by every days-outstanding metric. 365 (not 360) to
#: match the convention used by the Financial Ratios report's turnover ratios.
DAYS_IN_YEAR = 365.0


def _spec(**kwargs) -> MetricSpec:
	return MetricSpec(**kwargs)


# ---------------------------------------------------------------------------
# Liquidity / working capital
# ---------------------------------------------------------------------------
_LIQUIDITY = [
	_spec(
		key="current_ratio",
		label="Current Ratio",
		unit=RATIO,
		grain=GRAIN_COMPANY,
		direction=HIGHER_IS_BETTER,
		category=LIQUIDITY,
		description=(
			"Short-term solvency: how many times current assets cover current "
			"liabilities. Mirrors the 'Current Ratio' row of the Financial Ratios "
			"report (financial_ratios.py::add_liquidity_ratios -> calculate_ratio"
			"(current_asset, current_liability))."
		),
		formula_text="current_assets / current_liabilities",
		inputs=["current_assets", "current_liabilities"],
		fn=lambda v, c: c.div(v["current_assets"], v["current_liabilities"], "current_liabilities"),
		default_threshold=Threshold("current_ratio", green_min=2.0, amber_min=1.2),
	),
	_spec(
		key="quick_ratio",
		label="Quick Ratio",
		unit=RATIO,
		grain=GRAIN_COMPANY,
		direction=HIGHER_IS_BETTER,
		category=LIQUIDITY,
		description=(
			"Acid test: liquid assets (cash + bank + receivable ledger accounts) "
			"over current liabilities. Mirrors the 'Quick Ratio' row of the "
			"Financial Ratios report, including its definition of quick assets "
			"(financial_ratios.py::update_balances collects account_type in "
			"Bank/Cash/Receivable)."
		),
		formula_text="quick_assets / current_liabilities",
		inputs=["quick_assets", "current_liabilities"],
		fn=lambda v, c: c.div(v["quick_assets"], v["current_liabilities"], "current_liabilities"),
		default_threshold=Threshold("quick_ratio", green_min=1.0, amber_min=0.8),
	),
	_spec(
		key="working_capital",
		label="Working Capital",
		unit=CURRENCY,
		grain=GRAIN_COMPANY,
		direction=HIGHER_IS_BETTER,
		category=LIQUIDITY,
		description=(
			"Current assets less current liabilities, in company currency, as of "
			"the period end. Uses the same balances as current_ratio."
		),
		formula_text="current_assets - current_liabilities",
		inputs=["current_assets", "current_liabilities"],
		fn=lambda v, c: v["current_assets"] - v["current_liabilities"],
		# only a hard bound: negative working capital is Red, anything else Green
		default_threshold=Threshold("working_capital", red_max=0.0),
	),
	_spec(
		key="cash_balance",
		label="Cash and Bank Balance",
		unit=CURRENCY,
		grain=GRAIN_COMPANY,
		direction=HIGHER_IS_BETTER,
		category=LIQUIDITY,
		description=(
			"Closing balance of all ledger accounts of type Cash and Bank as of the "
			"period end (same source as the Cash Flow Forecast opening balance)."
		),
		formula_text="cash_and_bank",
		inputs=["cash_and_bank"],
		fn=lambda v, c: c.value(v["cash_and_bank"]),
	),
]

# ---------------------------------------------------------------------------
# Receivables / payables
# ---------------------------------------------------------------------------
_RECEIVABLES = [
	_spec(
		key="dso",
		label="Days Sales Outstanding",
		unit=DAYS,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=RECEIVABLES,
		description=(
			"Average days to collect a sale. Reciprocal of the Financial Ratios "
			"report's 'Debtor Turnover Ratio' (net_sales / avg_debtors) scaled to "
			"365 days; the receivable balance comes from the same "
			"utils.get_balance_on(account_type='Receivable') call."
		),
		formula_text="365 * ar_balance / revenue",
		inputs=["ar_balance", "revenue"],
		fn=lambda v, c: c.div(DAYS_IN_YEAR * v["ar_balance"], v["revenue"], "revenue"),
		default_threshold=Threshold("dso", green_min=45.0, amber_min=60.0),
	),
	_spec(
		key="dpo",
		label="Days Payables Outstanding",
		unit=DAYS,
		grain=GRAIN_COMPANY_PERIOD,
		direction=NEUTRAL,
		category=RECEIVABLES,
		description=(
			"Average days taken to pay suppliers. Direction is deliberately "
			"neutral: a high DPO conserves cash but can also signal strained "
			"payment behaviour, so no RAG judgement is shipped. Payable balance "
			"from utils.get_balance_on(account_type='Payable'), matching the "
			"Financial Ratios 'Creditor Turnover Ratio' source."
		),
		formula_text="365 * ap_balance / cogs",
		inputs=["ap_balance", "cogs"],
		fn=lambda v, c: c.div(DAYS_IN_YEAR * v["ap_balance"], v["cogs"], "cogs"),
	),
	_spec(
		key="dio",
		label="Days Inventory Outstanding",
		unit=DAYS,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=RECEIVABLES,
		description=(
			"Average days stock sits before it is consumed. Uses the closing stock "
			"balance (account_type='Stock'), unlike inventory_turns which uses the "
			"average balance the Financial Ratios report uses - the difference is "
			"documented in DESIGN.md."
		),
		formula_text="365 * inventory_balance / cogs",
		inputs=["inventory_balance", "cogs"],
		fn=lambda v, c: c.div(DAYS_IN_YEAR * v["inventory_balance"], v["cogs"], "cogs"),
	),
	_spec(
		key="cash_conversion_cycle",
		label="Cash Conversion Cycle",
		unit=DAYS,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=RECEIVABLES,
		description=(
			"DSO + DIO - DPO, computed from the same raw balances as those three "
			"metrics (never from their rounded values) so it cannot drift from "
			"them by more than the reported rounding."
		),
		formula_text="(365 * ar_balance / revenue) + (365 * inventory_balance / cogs) - (365 * ap_balance / cogs)",
		inputs=["ar_balance", "inventory_balance", "ap_balance", "revenue", "cogs"],
		fn=lambda v, c: _ccc(v, c),
	),
	_spec(
		key="ar_overdue_pct",
		label="Overdue Receivables %",
		unit=PERCENT,
		grain=GRAIN_COMPANY,
		direction=LOWER_IS_BETTER,
		category=RECEIVABLES,
		description=(
			"Share of the open receivable book that is past its due date, from "
			"Sales Invoice outstanding_amount vs due_date as of the period end - "
			"the same basis as the Accounts Receivable ageing report."
		),
		formula_text="100 * ar_overdue_outstanding / ar_total_outstanding",
		inputs=["ar_overdue_outstanding", "ar_total_outstanding"],
		fn=lambda v, c: c.pct(v["ar_overdue_outstanding"], v["ar_total_outstanding"], "ar_total_outstanding"),
		default_threshold=Threshold("ar_overdue_pct", green_min=10.0, amber_min=20.0),
	),
	_spec(
		key="collection_effectiveness_index",
		label="Collection Effectiveness Index",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=RECEIVABLES,
		description=(
			"CEI: how much of what was collectible in the period was actually "
			"collected. 100% means everything that came due was collected. "
			"Standard CRF definition; credit sales are approximated by period "
			"revenue."
		),
		formula_text=(
			"100 * (opening_ar_balance + revenue - ar_total_outstanding) / "
			"(opening_ar_balance + revenue - ar_current_outstanding)"
		),
		inputs=["opening_ar_balance", "revenue", "ar_total_outstanding", "ar_current_outstanding"],
		fn=lambda v, c: _cei(v, c),
		default_threshold=Threshold("collection_effectiveness_index", green_min=80.0, amber_min=60.0),
	),
]


def _ccc(v, c):
	dso = c.div(DAYS_IN_YEAR * v["ar_balance"], v["revenue"], "revenue")
	dio = c.div(DAYS_IN_YEAR * v["inventory_balance"], v["cogs"], "cogs")
	dpo = c.div(DAYS_IN_YEAR * v["ap_balance"], v["cogs"], "cogs")
	if dso is None or dio is None or dpo is None:
		return None
	return dso + dio - dpo


def _cei(v, c):
	collectible = v["opening_ar_balance"] + v["revenue"] - v["ar_current_outstanding"]
	collected = v["opening_ar_balance"] + v["revenue"] - v["ar_total_outstanding"]
	return c.pct(collected, collectible, "collectible base")


# ---------------------------------------------------------------------------
# Profitability
# ---------------------------------------------------------------------------
_PROFITABILITY = [
	_spec(
		key="revenue",
		label="Revenue",
		unit=CURRENCY,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=PROFITABILITY,
		description=(
			"Net income postings (credit - debit) on Income root_type accounts for "
			"the period, excluding Period Closing Voucher entries - identical to "
			"the 'Total Income (Credit)' row of the Profit and Loss Statement."
		),
		formula_text="revenue",
		inputs=["revenue"],
		fn=lambda v, c: c.value(v["revenue"]),
	),
	_spec(
		key="cogs",
		label="Cost of Goods Sold",
		unit=CURRENCY,
		grain=GRAIN_COMPANY_PERIOD,
		direction=NEUTRAL,
		category=PROFITABILITY,
		description=(
			"Expense postings on accounts with account_type = 'Cost of Goods Sold' "
			"- the same account selection the Financial Ratios report uses for its "
			"cogs bucket (financial_ratios.py::update_balances, root_type Expense / "
			"account_type 'Cost of Goods Sold')."
		),
		formula_text="cogs",
		inputs=["cogs"],
		fn=lambda v, c: c.value(v["cogs"]),
	),
	_spec(
		key="opex",
		label="Operating Expenses (excl. COGS)",
		unit=CURRENCY,
		grain=GRAIN_COMPANY_PERIOD,
		direction=NEUTRAL,
		category=PROFITABILITY,
		description=(
			"All Expense root_type postings for the period that are not Cost of "
			"Goods Sold. Together with cogs this reconciles to the P&L 'Total "
			"Expense (Debit)' row."
		),
		formula_text="opex",
		inputs=["opex"],
		fn=lambda v, c: c.value(v["opex"]),
	),
	_spec(
		key="gross_margin_pct",
		label="Gross Margin %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=PROFITABILITY,
		description=(
			"Mirrors the Financial Ratios 'Gross Profit Ratio' formula exactly "
			"((net_sales - cogs) / net_sales, financial_ratios.py::"
			"add_solvency_ratios) expressed as a percentage. Revenue here is total "
			"Income (P&L Total Income) rather than the Direct Income group the "
			"ratio report reads - see DESIGN.md."
		),
		formula_text="100 * (revenue - cogs) / revenue",
		inputs=["revenue", "cogs"],
		fn=lambda v, c: c.pct(v["revenue"] - v["cogs"], v["revenue"], "revenue"),
	),
	_spec(
		key="operating_margin_pct",
		label="Operating Margin %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=PROFITABILITY,
		description=(
			"EBIT-style margin: revenue less COGS less operating expenses, where "
			"operating expenses exclude Depreciation and Tax accounts. Chosen over "
			"an EBITDA proxy because ERPNext's chart of accounts does not reliably "
			"separate interest from other indirect expenses."
		),
		formula_text="100 * (revenue - cogs - operating_expense) / revenue",
		inputs=["revenue", "cogs", "operating_expense"],
		fn=lambda v, c: c.pct(v["revenue"] - v["cogs"] - v["operating_expense"], v["revenue"], "revenue"),
	),
	_spec(
		key="net_margin_pct",
		label="Net Margin %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=PROFITABILITY,
		description=(
			"Mirrors the Financial Ratios 'Net Profit Ratio' exactly "
			"(profit_after_tax / net_sales where profit_after_tax = total income - "
			"total expense, financial_ratios.py::add_solvency_ratios); here total "
			"expense = cogs + opex, expressed as a percentage."
		),
		formula_text="100 * (revenue - cogs - opex) / revenue",
		inputs=["revenue", "cogs", "opex"],
		fn=lambda v, c: c.pct(v["revenue"] - v["cogs"] - v["opex"], v["revenue"], "revenue"),
	),
]

# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------
_GROWTH = [
	_spec(
		key="revenue_growth_pct",
		label="Revenue Growth %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=GROWTH,
		description=(
			"Period revenue against the immediately preceding period of equal "
			"length (the same prior-period window the KPI Scorecard uses for its "
			"trend columns). Undefined when the prior period had no revenue."
		),
		formula_text="100 * (revenue - prior_revenue) / prior_revenue",
		inputs=["revenue", "prior_revenue"],
		fn=lambda v, c: c.pct(v["revenue"] - v["prior_revenue"], v["prior_revenue"], "prior_revenue"),
	),
]

# ---------------------------------------------------------------------------
# Operations / supply
# ---------------------------------------------------------------------------
_OPERATIONS = [
	_spec(
		key="inventory_turns",
		label="Inventory Turns",
		unit=RATIO,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=OPERATIONS,
		description=(
			"Mirrors the Financial Ratios 'Inventory Turnover Ratio' exactly: cogs "
			"over the average stock balance, where the average is (opening + "
			"closing) / 2 from utils.get_balance_on(account_type='Stock') "
			"(financial_ratios.py::avg_ratio_balance)."
		),
		formula_text="cogs / average_inventory",
		inputs=["cogs", "average_inventory"],
		fn=lambda v, c: c.div(v["cogs"], v["average_inventory"], "average_inventory"),
	),
	_spec(
		key="otif_pct",
		label="On-Time In-Full %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=HIGHER_IS_BETTER,
		category=OPERATIONS,
		description=(
			"Share of sales order lines delivered in the period that were both on "
			"time (last delivery note posting date <= the line's promised "
			"delivery_date) and in full (delivered_qty >= ordered qty). Line-level, "
			"not order-level - see DESIGN.md for the exact definition."
		),
		formula_text="100 * otif_lines / delivered_lines",
		inputs=["otif_lines", "delivered_lines"],
		fn=lambda v, c: c.pct(v["otif_lines"], v["delivered_lines"], "delivered_lines"),
		default_threshold=Threshold("otif_pct", green_min=95.0, amber_min=90.0),
	),
	_spec(
		key="purchase_price_variance_pct",
		label="Purchase Price Variance %",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=OPERATIONS,
		description=(
			"Receipt-time price variance: value received in the period at the "
			"actual receipt rate versus the same quantity at the purchase order "
			"rate. Positive = paying more than ordered."
		),
		formula_text="100 * ppv_variance_amount / ppv_baseline_amount",
		inputs=["ppv_variance_amount", "ppv_baseline_amount"],
		fn=lambda v, c: c.pct(v["ppv_variance_amount"], v["ppv_baseline_amount"], "ppv_baseline_amount"),
		default_threshold=Threshold("purchase_price_variance_pct", green_min=1.0, amber_min=3.0),
	),
]

# ---------------------------------------------------------------------------
# Governance / close
# ---------------------------------------------------------------------------
_GOVERNANCE = [
	_spec(
		key="close_duration_days",
		label="Close Duration (Days)",
		unit=DAYS,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=GOVERNANCE,
		description=(
			"Average days from period end to the last Close Task sign-off, over "
			"the Completed Close Cycles whose period end falls in the window. "
			"Reads Close Cycle / Close Task built earlier in this branch."
		),
		formula_text="close_cycle_days_total / close_cycles_completed",
		inputs=["close_cycle_days_total", "close_cycles_completed"],
		fn=lambda v, c: c.div(v["close_cycle_days_total"], v["close_cycles_completed"], "closed cycles"),
		default_threshold=Threshold("close_duration_days", green_min=5.0, amber_min=10.0),
	),
	_spec(
		key="open_high_anomalies",
		label="Open High-Severity Anomalies",
		unit=COUNT,
		grain=GRAIN_COMPANY,
		direction=LOWER_IS_BETTER,
		category=GOVERNANCE,
		description=(
			"Anomaly Findings with severity 'High' still in status 'Open' or "
			"'Investigating' for the company (anomaly detection, this branch)."
		),
		formula_text="open_high_anomalies",
		inputs=["open_high_anomalies"],
		fn=lambda v, c: c.value(v["open_high_anomalies"]),
		default_threshold=Threshold("open_high_anomalies", green_min=0.0, amber_min=5.0),
	),
	_spec(
		key="sod_open_violations",
		label="Open SoD Violations",
		unit=COUNT,
		grain=GRAIN_COMPANY,
		direction=LOWER_IS_BETTER,
		category=GOVERNANCE,
		description=(
			"SoD Violation Log rows still in status 'Open'. Segregation-of-duties "
			"conflicts are user-scoped, not company-scoped, so this metric is the "
			"same for every company on the site (noted as a warning by the loader)."
		),
		formula_text="open_sod_violations",
		inputs=["open_sod_violations"],
		fn=lambda v, c: c.value(v["open_sod_violations"]),
		default_threshold=Threshold("sod_open_violations", green_min=0.0, amber_min=3.0),
	),
	_spec(
		key="forecast_accuracy_mape",
		label="Forecast Accuracy (MAPE)",
		unit=PERCENT,
		grain=GRAIN_COMPANY_PERIOD,
		direction=LOWER_IS_BETTER,
		category=GOVERNANCE,
		description=(
			"Mean absolute percentage error of the most recent Sales Forecast for "
			"the company, averaged over its item rows' stored mape (wave-2 "
			"statistical forecasting writes this per Sales Forecast Item). Lower "
			"is better; 0 would be a perfect forecast."
		),
		formula_text="forecast_mape_sum / forecast_mape_count",
		inputs=["forecast_mape_sum", "forecast_mape_count"],
		fn=lambda v, c: c.div(v["forecast_mape_sum"], v["forecast_mape_count"], "forecast rows"),
		default_threshold=Threshold("forecast_accuracy_mape", green_min=20.0, amber_min=30.0),
	),
]


ALL_SPECS: tuple[MetricSpec, ...] = tuple(
	_LIQUIDITY + _RECEIVABLES + _PROFITABILITY + _GROWTH + _OPERATIONS + _GOVERNANCE
)

#: the catalog, keyed by metric key, in category order
METRICS: dict[str, MetricSpec] = {spec.key: spec for spec in ALL_SPECS}


def get_spec(key: str) -> MetricSpec | None:
	"""Return the spec for ``key`` or ``None`` if it is not in the catalog."""
	return METRICS.get(key)


def get_specs(keys=None) -> list[MetricSpec]:
	"""Return specs for ``keys`` (unknown keys skipped), or the whole catalog."""
	if not keys:
		return list(ALL_SPECS)
	return [METRICS[key] for key in keys if key in METRICS]


def specs_by_category(keys=None) -> dict[str, list[MetricSpec]]:
	grouped: dict[str, list[MetricSpec]] = {}
	for spec in get_specs(keys):
		grouped.setdefault(spec.category, []).append(spec)
	return grouped


def all_inputs() -> list[str]:
	"""Every named input the pack needs - the loaders' contract."""
	names: list[str] = []
	for spec in ALL_SPECS:
		for name in spec.inputs:
			if name not in names:
				names.append(name)
	return names


def catalog_as_dicts(keys=None) -> list[dict]:
	"""Serializable catalog for UIs, fixtures and agents."""
	return [spec.as_dict() for spec in get_specs(keys)]


def validate_registry() -> list[str]:
	"""Return every structural problem in the pack (empty == healthy)."""
	problems: list[str] = []
	seen: set[str] = set()
	for spec in ALL_SPECS:
		if spec.key in seen:
			problems.append(f"duplicate metric key {spec.key!r}")
		seen.add(spec.key)
		problems.extend(spec.validate())
	return problems
