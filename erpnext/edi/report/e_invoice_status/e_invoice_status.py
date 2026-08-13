# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""E-Invoice Status: every submitted Sales Invoice in the period, joined to
its latest EDI Transmission Log — invoices without a log show as
"Not Generated" so gaps in a country mandate are immediately visible."""

import frappe
from frappe import _

NOT_GENERATED = "Not Generated"
STATUS_ORDER = [NOT_GENERATED, "Generated", "Queued", "Transmitted", "Failed"]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	return columns, data, None, chart


def get_columns():
	return [
		{
			"label": _("Sales Invoice"),
			"fieldname": "sales_invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 180,
		},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 160,
		},
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 105,
		},
		{
			"label": _("Grand Total"),
			"fieldname": "grand_total",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
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
			"label": _("UBL Profile"),
			"fieldname": "ubl_profile",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 110,
		},
		{
			"label": _("Log"),
			"fieldname": "log_name",
			"fieldtype": "Link",
			"options": "EDI Transmission Log",
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
		{
			"label": _("Message"),
			"fieldname": "message",
			"fieldtype": "Data",
			"width": 240,
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
		conditions += " and si.company = %(company)s"
	if filters.get("from_date"):
		conditions += " and si.posting_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " and si.posting_date <= %(to_date)s"

	status_condition = ""
	if filters.get("status") == NOT_GENERATED:
		status_condition = " and log.name is null"
	elif filters.get("status"):
		status_condition = " and log.status = %(status)s"

	return frappe.db.sql(
		f"""
		select
			si.name as sales_invoice,
			si.customer,
			si.posting_date,
			si.grand_total,
			si.currency,
			log.ubl_profile,
			coalesce(log.status, %(not_generated)s) as status,
			log.name as log_name,
			log.generated_on,
			log.transmitted_on,
			log.message
		from `tabSales Invoice` si
		left join (
			select l.*
			from `tabEDI Transmission Log` l
			inner join (
				select sales_invoice, max(creation) as max_creation
				from `tabEDI Transmission Log`
				group by sales_invoice
			) latest
				on latest.sales_invoice = l.sales_invoice
				and latest.max_creation = l.creation
		) log on log.sales_invoice = si.name
		where si.docstatus = 1 {conditions}
		{status_condition}
		order by si.posting_date desc, si.name desc
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
			"datasets": [{"name": _("Invoices"), "values": list(counts.values())}],
		},
		"type": "bar",
		"colors": ["#ecad4b", "#7575ff", "#5e64ff", "#28a745", "#ff5858"],
	}
