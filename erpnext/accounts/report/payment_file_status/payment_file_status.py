# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Payment File Status: every submitted Payment Order in the period, joined to
its latest Bank Payment File Log.

Payment Orders without a log show as "Not Generated" — those are batched
payments that were approved but never actually sent to a bank, which is the
gap this report exists to make visible.

``total_amount`` is the plain sum of the Payment Order's row amounts. A
Payment Order normally debits one account in one currency, but nothing in the
doctype enforces that, so treat the total as indicative and read the authoritative
control sum off the log (which is what was written into the file).
"""

import frappe
from frappe import _

NOT_GENERATED = "Not Generated"
STATUS_ORDER = [NOT_GENERATED, "Generated", "Transmitted", "Failed"]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	return columns, data, None, chart


def get_columns():
	return [
		{
			"label": _("Payment Order"),
			"fieldname": "payment_order",
			"fieldtype": "Link",
			"options": "Payment Order",
			"width": 150,
		},
		{
			"label": _("Company"),
			"fieldname": "company",
			"fieldtype": "Link",
			"options": "Company",
			"width": 160,
		},
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 105,
		},
		{
			"label": _("Bank Account"),
			"fieldname": "company_bank_account",
			"fieldtype": "Link",
			"options": "Bank Account",
			"width": 160,
		},
		{
			"label": _("Total Amount"),
			"fieldname": "total_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 130,
		},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"width": 80,
			"hidden": 1,
		},
		{
			"label": _("Transactions"),
			"fieldname": "number_of_transactions",
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"label": _("File Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Variant"),
			"fieldname": "pain_variant",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Log"),
			"fieldname": "log_name",
			"fieldtype": "Link",
			"options": "Bank Payment File Log",
			"width": 120,
		},
		{
			"label": _("Generated On"),
			"fieldname": "generated_on",
			"fieldtype": "Datetime",
			"width": 150,
		},
		{
			"label": _("Transmitted On"),
			"fieldname": "transmitted_on",
			"fieldtype": "Datetime",
			"width": 150,
		},
	]


def get_data(filters):
	conditions = ""
	values = {
		"not_generated": NOT_GENERATED,
		"company": filters.get("company"),
		"from_date": filters.get("from_date"),
		"to_date": filters.get("to_date"),
		"status": filters.get("status"),
	}

	if filters.get("company"):
		conditions += " and po.company = %(company)s"
	if filters.get("from_date"):
		conditions += " and po.posting_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " and po.posting_date <= %(to_date)s"

	status_condition = ""
	if filters.get("status") == NOT_GENERATED:
		status_condition = " and log.name is null"
	elif filters.get("status"):
		status_condition = " and log.status = %(status)s"

	return frappe.db.sql(
		f"""
		select
			po.name as payment_order,
			po.company,
			po.posting_date,
			po.company_bank_account,
			totals.total_amount,
			coalesce(log.number_of_transactions, totals.row_count) as number_of_transactions,
			coalesce(log.status, %(not_generated)s) as status,
			log.pain_variant,
			log.currency,
			log.name as log_name,
			log.generated_on,
			log.transmitted_on
		from `tabPayment Order` po
		left join (
			select parent, sum(amount) as total_amount, count(name) as row_count
			from `tabPayment Order Reference`
			where parenttype = 'Payment Order'
			group by parent
		) totals on totals.parent = po.name
		left join (
			select l.*
			from `tabBank Payment File Log` l
			inner join (
				select payment_order, max(creation) as max_creation
				from `tabBank Payment File Log`
				group by payment_order
			) latest
				on latest.payment_order = l.payment_order
				and latest.max_creation = l.creation
		) log on log.payment_order = po.name
		where po.docstatus = 1 {conditions}
		{status_condition}
		order by po.posting_date desc, po.name desc
		""",
		values,
		as_dict=True,
	)


def get_chart(data):
	counts = {status: 0 for status in STATUS_ORDER}
	for row in data:
		counts[row.status] = counts.get(row.status, 0) + 1

	return {
		"data": {
			"labels": [_(status) for status in counts],
			"datasets": [{"name": _("Payment Orders"), "values": list(counts.values())}],
		},
		"type": "bar",
		"colors": ["#ecad4b", "#5e64ff", "#28a745", "#ff5858"],
	}
