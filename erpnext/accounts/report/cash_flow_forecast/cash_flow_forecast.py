# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from erpnext.accounts.forecasting.engine import build_forecast
from erpnext.accounts.forecasting.loaders import (
	build_cash_flow_items,
	get_company_currency,
	get_opening_balance,
	get_scenario,
)


def execute(filters=None):
	return CashFlowForecastReport(filters).run()


class CashFlowForecastReport:
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.company_currency = get_company_currency(self.filters.company)

	def run(self):
		from_date = getdate(self.filters.from_date)
		to_date = getdate(self.filters.to_date)

		items = build_cash_flow_items(self.filters)
		opening_balance = get_opening_balance(self.filters.company, add_days(from_date, -1))

		self.result = build_forecast(
			items,
			opening_balance,
			from_date,
			to_date,
			periodicity=self.filters.periodicity or "Weekly",
			scenario=get_scenario(self.filters),
		)

		return self.get_columns(), self.get_data(), None, self.get_chart(), self.get_summary()

	def get_columns(self):
		columns = [
			{
				"label": _("Cash Flow"),
				"fieldname": "cash_flow",
				"fieldtype": "Data",
				"width": 220,
			}
		]

		width = 100 if self.filters.periodicity == "Daily" else 150
		for period in self.result.periods:
			columns.append(
				{
					"label": _(period.label),
					"fieldname": period.key,
					"fieldtype": "Currency",
					"options": "currency",
					"width": width,
				}
			)

		columns.append(
			{
				"label": _("Total"),
				"fieldname": "total",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 150,
			}
		)

		return columns

	def get_data(self):
		result = self.result
		data = []

		opening_row = self.make_row(_("Opening Balance"), bold=1)
		for period in result.periods:
			opening_row[period.key] = period.opening_balance
		opening_row["total"] = result.opening_balance
		data.append(opening_row)

		for source_type in result.inflow_source_types():
			data.append(self.make_source_row(source_type, "inflows", indent=1))
		data.append(self.make_total_row(_("Total Inflows"), "total_inflow"))

		for source_type in result.outflow_source_types():
			data.append(self.make_source_row(source_type, "outflows", indent=1, negate=True))
		data.append(self.make_total_row(_("Total Outflows"), "total_outflow", negate=True))

		net_row = self.make_row(_("Net Cash Flow"), bold=1)
		for period in result.periods:
			net_row[period.key] = period.net
		net_row["total"] = sum(period.net for period in result.periods)
		data.append(net_row)

		closing_row = self.make_row(_("Closing Balance"), bold=1)
		for period in result.periods:
			closing_row[period.key] = period.closing_balance
		closing_row["total"] = result.closing_balance
		data.append(closing_row)

		return data

	def make_row(self, label, bold=0, indent=0):
		return {
			"cash_flow": label,
			"bold": bold,
			"indent": indent,
			"currency": self.company_currency,
		}

	def make_source_row(self, source_type, side, indent=0, negate=False):
		sign = -1 if negate else 1
		row = self.make_row(_(source_type), indent=indent)
		total = 0.0
		for period in self.result.periods:
			amount = getattr(period, side).get(source_type, 0.0)
			row[period.key] = sign * amount
			total += amount
		row["total"] = sign * total
		return row

	def make_total_row(self, label, attr, negate=False):
		sign = -1 if negate else 1
		row = self.make_row(label, bold=1)
		total = 0.0
		for period in self.result.periods:
			amount = getattr(period, attr)
			row[period.key] = sign * amount
			total += amount
		row["total"] = sign * total
		return row

	def get_chart(self):
		return {
			"data": {
				"labels": [_(period.label) for period in self.result.periods],
				"datasets": [
					{
						"name": _("Closing Balance"),
						"values": [period.closing_balance for period in self.result.periods],
					},
					{
						"name": _("Net Cash Flow"),
						"values": [period.net for period in self.result.periods],
						"chartType": "bar",
					},
				],
			},
			"type": "line",
			"fieldtype": "Currency",
			"options": "currency",
			"currency": self.company_currency,
		}

	def get_summary(self):
		periods = self.result.periods
		return [
			{
				"value": self.result.opening_balance,
				"label": _("Opening Balance"),
				"datatype": "Currency",
				"currency": self.company_currency,
			},
			{
				"value": sum(period.total_inflow for period in periods),
				"label": _("Total Inflows"),
				"indicator": "Green",
				"datatype": "Currency",
				"currency": self.company_currency,
			},
			{
				"value": sum(period.total_outflow for period in periods),
				"label": _("Total Outflows"),
				"indicator": "Red",
				"datatype": "Currency",
				"currency": self.company_currency,
			},
			{
				"value": self.result.closing_balance,
				"label": _("Projected Closing Balance"),
				"indicator": "Green" if self.result.closing_balance >= 0 else "Red",
				"datatype": "Currency",
				"currency": self.company_currency,
			},
		]
