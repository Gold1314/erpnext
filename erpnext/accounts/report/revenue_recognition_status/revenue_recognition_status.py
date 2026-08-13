# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_months, flt, get_first_day, get_last_day, getdate


def execute(filters=None):
	return RevenueRecognitionStatusReport(filters).run()


class RevenueRecognitionStatusReport:
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.as_of = getdate(self.filters.as_of) if self.filters.as_of else getdate()

	def run(self):
		self.load()
		data = self.get_data()
		return self.get_columns(), data, None, self.get_chart(), self.get_summary(data)

	def load(self):
		contract_filters = {"docstatus": 1}
		if self.filters.company:
			contract_filters["company"] = self.filters.company
		if self.filters.customer:
			contract_filters["customer"] = self.filters.customer
		if self.filters.revenue_contract:
			contract_filters["name"] = self.filters.revenue_contract

		self.contracts = frappe.get_all(
			"Revenue Contract",
			filters=contract_filters,
			fields=["name", "customer", "company", "status", "transaction_price"],
			order_by="name",
		)
		contract_names = [c.name for c in self.contracts]

		self.obligations = (
			frappe.get_all(
				"Revenue Contract Obligation",
				filters={"parenttype": "Revenue Contract", "parent": ("in", contract_names)},
				fields=[
					"parent",
					"idx",
					"description",
					"satisfaction_method",
					"allocated_amount",
					"recognized_amount",
					"satisfied",
				],
				order_by="parent, idx",
			)
			if contract_names
			else []
		)

		self.plan_rows = (
			frappe.get_all(
				"Revenue Recognition Entry",
				filters={"parenttype": "Revenue Contract", "parent": ("in", contract_names)},
				fields=["parent", "obligation_idx", "period_start", "period_end", "amount", "posted"],
				order_by="parent, period_end",
			)
			if contract_names
			else []
		)

	def get_columns(self):
		return [
			{
				"label": _("Customer"),
				"fieldname": "customer",
				"fieldtype": "Link",
				"options": "Customer",
				"width": 160,
			},
			{
				"label": _("Revenue Contract"),
				"fieldname": "revenue_contract",
				"fieldtype": "Link",
				"options": "Revenue Contract",
				"width": 150,
			},
			{"label": _("Obligation"), "fieldname": "obligation", "fieldtype": "Data", "width": 220},
			{"label": _("Method"), "fieldname": "method", "fieldtype": "Data", "width": 110},
			{"label": _("Allocated"), "fieldname": "allocated", "fieldtype": "Currency", "width": 120},
			{"label": _("Recognized"), "fieldname": "recognized", "fieldtype": "Currency", "width": 120},
			{
				"label": _("Deferred Balance"),
				"fieldname": "deferred_balance",
				"fieldtype": "Currency",
				"width": 130,
			},
			{
				"label": _("% Complete"),
				"fieldname": "percent_complete",
				"fieldtype": "Percent",
				"width": 100,
			},
			{
				"label": _("Next Unposted Period"),
				"fieldname": "next_period",
				"fieldtype": "Date",
				"width": 140,
			},
		]

	def get_data(self):
		customer_by_contract = {c.name: c.customer for c in self.contracts}

		next_period = {}
		for row in self.plan_rows:
			if row.posted or not row.period_end:
				continue
			key = (row.parent, row.obligation_idx)
			if key not in next_period or getdate(row.period_end) < getdate(next_period[key]):
				next_period[key] = row.period_end

		data = []
		for obligation in self.obligations:
			allocated = flt(obligation.allocated_amount, 2)
			recognized = flt(obligation.recognized_amount, 2)
			data.append(
				{
					"customer": customer_by_contract.get(obligation.parent),
					"revenue_contract": obligation.parent,
					"obligation": obligation.description,
					"method": obligation.satisfaction_method,
					"allocated": allocated,
					"recognized": recognized,
					"deferred_balance": flt(allocated - recognized, 2),
					"percent_complete": flt(recognized / allocated * 100, 2) if allocated else 0,
					"next_period": next_period.get((obligation.parent, obligation.idx)),
				}
			)
		return data

	def get_chart(self):
		"""Recognized (posted) vs still-deferred (unposted) plan amounts, next 12 months."""
		months = []
		labels = []
		start = get_first_day(self.as_of)
		for offset in range(12):
			month_start = get_first_day(add_months(start, offset))
			months.append((month_start, get_last_day(month_start)))
			labels.append(month_start.strftime("%b %Y"))

		recognized = [0.0] * 12
		deferred = [0.0] * 12
		for row in self.plan_rows:
			if not row.period_end:
				continue
			period_end = getdate(row.period_end)
			for index, (month_start, month_end) in enumerate(months):
				if month_start <= period_end <= month_end:
					bucket = recognized if row.posted else deferred
					bucket[index] = flt(bucket[index] + flt(row.amount), 2)
					break

		return {
			"data": {
				"labels": labels,
				"datasets": [
					{"name": _("Recognized"), "values": recognized},
					{"name": _("Deferred"), "values": deferred},
				],
			},
			"type": "bar",
			"barOptions": {"stacked": 1},
			"colors": ["#29a3a3", "#ffa00a"],
		}

	def get_summary(self, data):
		total_allocated = flt(sum(row["allocated"] for row in data), 2)
		total_recognized = flt(sum(row["recognized"] for row in data), 2)
		return [
			{
				"value": total_allocated,
				"label": _("Total Allocated"),
				"datatype": "Currency",
				"indicator": "blue",
			},
			{
				"value": total_recognized,
				"label": _("Total Recognized"),
				"datatype": "Currency",
				"indicator": "green",
			},
			{
				"value": flt(total_allocated - total_recognized, 2),
				"label": _("Total Deferred"),
				"datatype": "Currency",
				"indicator": "orange",
			},
		]
