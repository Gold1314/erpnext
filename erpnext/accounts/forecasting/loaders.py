# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe-side loaders for the cash-flow forecasting engine.

All DB access lives here; the engine (``engine.py``) stays pure. Every loader
returns ``list[CashFlowItem]`` with amounts in the **company currency**
(positive = inflow, negative = outflow).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, add_months, add_years, cint, flt, getdate

from erpnext.accounts.forecasting.models import (
	PAYABLE,
	PURCHASE_ORDER,
	RECEIVABLE,
	SALES_ORDER,
	SUBSCRIPTION,
	CashFlowItem,
	ForecastScenario,
)

ACTIVE_SUBSCRIPTION_STATUSES = ("Active",)


def build_cash_flow_items(filters) -> list[CashFlowItem]:
	"""Assemble all sources for the report per scenario flags.

	``filters`` needs: company, to_date; optional include_sales_orders,
	include_purchase_orders.
	"""
	filters = frappe._dict(filters or {})
	horizon_end = getdate(filters.to_date)

	items = []
	items += get_receivables(filters.company)
	items += get_payables(filters.company)
	if cint(filters.get("include_sales_orders")):
		items += get_sales_order_pipeline(filters.company)
	if cint(filters.get("include_purchase_orders")):
		items += get_purchase_order_pipeline(filters.company)
	items += get_subscription_inflows(filters.company, horizon_end)

	return items


def get_scenario(filters) -> ForecastScenario:
	filters = frappe._dict(filters or {})
	return ForecastScenario(
		receivable_delay_days=cint(filters.get("receivable_delay_days")),
		payable_delay_days=cint(filters.get("payable_delay_days")),
		include_sales_orders=bool(cint(filters.get("include_sales_orders"))),
		include_purchase_orders=bool(cint(filters.get("include_purchase_orders"))),
		confidence_haircut_pct=flt(filters.get("pipeline_haircut_pct")),
	)


# ---------------------------------------------------------------------------
# Receivables / Payables (Payment Schedule rows on outstanding invoices,
# falling back to invoice-level due_date/outstanding_amount)
# ---------------------------------------------------------------------------


def get_receivables(company: str) -> list[CashFlowItem]:
	return get_invoice_items(company, "Sales Invoice", RECEIVABLE, sign=1)


def get_payables(company: str) -> list[CashFlowItem]:
	return get_invoice_items(company, "Purchase Invoice", PAYABLE, sign=-1)


def get_invoice_items(company: str, doctype: str, source_type: str, sign: int) -> list[CashFlowItem]:
	party_type = "Customer" if doctype == "Sales Invoice" else "Supplier"
	party_field = "customer" if doctype == "Sales Invoice" else "supplier"
	company_currency = get_company_currency(company)

	items = []
	covered_invoices = set()

	# 1) Payment Schedule rows: per-row unpaid portion at the row's due date.
	for row in get_payment_schedule_rows(company, doctype, party_field):
		covered_invoices.add(row.invoice)
		unpaid = flt(row.payment_amount) - flt(row.paid_amount) - flt(row.discounted_amount)
		if unpaid <= 0:
			continue

		items.append(
			CashFlowItem(
				posting_date=getdate(row.due_date),
				amount=sign * unpaid * flt(row.conversion_rate or 1),
				source_type=source_type,
				party=row.party,
				party_type=party_type,
				reference_doctype=doctype,
				reference_name=row.invoice,
				currency=company_currency,
			)
		)

	# 2) Invoices without a payment schedule: invoice-level due date/outstanding.
	for row in get_invoices_without_schedule(company, doctype, party_field, covered_invoices):
		outstanding = flt(row.outstanding_amount)
		if row.party_account_currency and row.party_account_currency != company_currency:
			outstanding *= flt(row.conversion_rate or 1)

		items.append(
			CashFlowItem(
				posting_date=getdate(row.due_date or row.posting_date),
				amount=sign * outstanding,
				source_type=source_type,
				party=row.party,
				party_type=party_type,
				reference_doctype=doctype,
				reference_name=row.name,
				currency=company_currency,
			)
		)

	return items


def get_payment_schedule_rows(company, doctype, party_field):
	schedule = frappe.qb.DocType("Payment Schedule")
	invoice = frappe.qb.DocType(doctype)

	return (
		frappe.qb.from_(schedule)
		.join(invoice)
		.on(schedule.parent == invoice.name)
		.select(
			invoice.name.as_("invoice"),
			invoice[party_field].as_("party"),
			invoice.conversion_rate,
			schedule.due_date,
			schedule.payment_amount,
			schedule.paid_amount,
			schedule.discounted_amount,
		)
		.where(
			(schedule.parenttype == doctype)
			& (invoice.docstatus == 1)
			& (invoice.outstanding_amount > 0)
			& (invoice.company == company)
		)
		.orderby(schedule.due_date)
	).run(as_dict=True)


def get_invoices_without_schedule(company, doctype, party_field, covered_invoices):
	invoice = frappe.qb.DocType(doctype)

	query = (
		frappe.qb.from_(invoice)
		.select(
			invoice.name,
			invoice[party_field].as_("party"),
			invoice.due_date,
			invoice.posting_date,
			invoice.outstanding_amount,
			invoice.party_account_currency,
			invoice.conversion_rate,
		)
		.where((invoice.docstatus == 1) & (invoice.outstanding_amount > 0) & (invoice.company == company))
	)
	if covered_invoices:
		query = query.where(invoice.name.notin(list(covered_invoices)))

	return query.run(as_dict=True)


# ---------------------------------------------------------------------------
# Sales Order / Purchase Order pipeline (unbilled portion)
# ---------------------------------------------------------------------------


def get_sales_order_pipeline(company: str) -> list[CashFlowItem]:
	return get_order_pipeline(
		company,
		doctype="Sales Order",
		source_type=SALES_ORDER,
		sign=1,
		party_type="Customer",
		party_field="customer",
		date_field="delivery_date",
	)


def get_purchase_order_pipeline(company: str) -> list[CashFlowItem]:
	return get_order_pipeline(
		company,
		doctype="Purchase Order",
		source_type=PURCHASE_ORDER,
		sign=-1,
		party_type="Supplier",
		party_field="supplier",
		date_field="schedule_date",
	)


def get_order_pipeline(
	company, doctype, source_type, sign, party_type, party_field, date_field
) -> list[CashFlowItem]:
	order = frappe.qb.DocType(doctype)
	company_currency = get_company_currency(company)

	rows = (
		frappe.qb.from_(order)
		.select(
			order.name,
			order[party_field].as_("party"),
			order[date_field].as_("expected_date"),
			order.transaction_date,
			order.base_grand_total,
			order.per_billed,
		)
		.where(
			(order.docstatus == 1)
			& (order.company == company)
			& (order.status.notin(["Closed", "On Hold"]))
			& (order.per_billed < 100)
		)
	).run(as_dict=True)

	items = []
	for row in rows:
		remaining = flt(row.base_grand_total) * (100.0 - flt(row.per_billed)) / 100.0
		if remaining <= 0:
			continue

		items.append(
			CashFlowItem(
				posting_date=getdate(row.expected_date or row.transaction_date),
				amount=sign * remaining,
				source_type=source_type,
				party=row.party,
				party_type=party_type,
				reference_doctype=doctype,
				reference_name=row.name,
				currency=company_currency,
			)
		)

	return items


# ---------------------------------------------------------------------------
# Subscriptions (recurring billing projected over the horizon)
# ---------------------------------------------------------------------------


def get_subscription_inflows(company: str, horizon_end) -> list[CashFlowItem]:
	"""Project recurring subscription billings up to ``horizon_end``.

	Customer subscriptions produce inflows, Supplier subscriptions outflows.
	Each cycle's amount is sum(qty x Subscription Plan.cost) converted to the
	company currency at 1:1 when the plan currency matches, otherwise the row
	is skipped (multi-currency plan conversion is out of scope for v1).
	"""
	horizon_end = getdate(horizon_end)
	company_currency = get_company_currency(company)

	subscriptions = frappe.get_all(
		"Subscription",
		filters={"company": company, "status": ("in", ACTIVE_SUBSCRIPTION_STATUSES)},
		fields=[
			"name",
			"party_type",
			"party",
			"current_invoice_end",
			"next_billing_period_end",
			"end_date",
			"days_until_due",
		],
	)
	if not subscriptions:
		return []

	plan_rows = frappe.get_all(
		"Subscription Plan Detail",
		filters={"parenttype": "Subscription", "parent": ("in", [d.name for d in subscriptions])},
		fields=["parent", "qty", "plan"],
	)
	plans = {
		plan.name: plan
		for plan in frappe.get_all(
			"Subscription Plan",
			filters={"name": ("in", list({row.plan for row in plan_rows}))},
			fields=["name", "cost", "currency", "billing_interval", "billing_interval_count"],
		)
	}

	items = []
	for subscription in subscriptions:
		rows = [row for row in plan_rows if row.parent == subscription.name]
		if not rows:
			continue

		amount_per_cycle = 0.0
		for row in rows:
			plan = plans.get(row.plan)
			if not plan or (plan.currency and plan.currency != company_currency):
				continue
			amount_per_cycle += flt(row.qty) * flt(plan.cost)

		if amount_per_cycle <= 0:
			continue

		# all plans on one subscription share a billing interval (validated
		# by the Subscription doctype) - read it off the first resolvable plan
		plan = next((plans[row.plan] for row in rows if row.plan in plans), None)
		if not plan:
			continue

		sign = -1 if subscription.party_type == "Supplier" else 1
		billing_date = getdate(subscription.current_invoice_end or subscription.next_billing_period_end)
		if not billing_date:
			continue

		subscription_end = getdate(subscription.end_date) if subscription.end_date else None
		due_lag = cint(subscription.days_until_due)

		while billing_date <= horizon_end:
			if subscription_end and billing_date > subscription_end:
				break

			items.append(
				CashFlowItem(
					posting_date=add_days(billing_date, due_lag),
					amount=sign * amount_per_cycle,
					source_type=SUBSCRIPTION,
					party=subscription.party,
					party_type=subscription.party_type,
					reference_doctype="Subscription",
					reference_name=subscription.name,
					currency=company_currency,
				)
			)
			billing_date = advance_billing_date(
				billing_date, plan.billing_interval, plan.billing_interval_count
			)

	return items


def advance_billing_date(billing_date, interval, count):
	count = cint(count) or 1
	if interval == "Day":
		return add_days(billing_date, count)
	if interval == "Week":
		return add_days(billing_date, 7 * count)
	if interval == "Year":
		return add_years(billing_date, count)
	return add_months(billing_date, count)  # Month (default)


# ---------------------------------------------------------------------------
# Opening balance (Bank + Cash GL balance as of a date)
# ---------------------------------------------------------------------------


def get_opening_balance(company: str, as_of) -> float:
	"""Company-currency balance of all Bank/Cash accounts as of ``as_of``
	(inclusive), mirroring erpnext.accounts.utils.get_balance_on semantics
	(sum(debit) - sum(credit) over non-cancelled GL Entries)."""
	balance = frappe.db.sql(
		"""
		select sum(gle.debit) - sum(gle.credit)
		from `tabGL Entry` gle
		inner join `tabAccount` account on account.name = gle.account
		where gle.company = %(company)s
			and gle.is_cancelled = 0
			and gle.posting_date <= %(as_of)s
			and account.account_type in ('Bank', 'Cash')
		""",
		{"company": company, "as_of": getdate(as_of)},
	)

	return flt(balance[0][0]) if balance else 0.0


def get_company_currency(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "default_currency")
