# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Cycle Count Summary - accuracy/coverage dashboard over Cycle Count Log.

One row per log (i.e. per program+item): class, velocity, last counted, next
due, days overdue and the linked count sheet. "Overdue" means the next due
date has passed as of today; the on-time percentage in the summary cards is
the share of items that are *not* overdue (counted recently enough, or not
yet due).
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, nowdate

STATUSES = ("Classified", "Scheduled", "Counted")


def execute(filters=None):
	return CycleCountSummary(filters).run()


class CycleCountSummary:
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})

	def run(self):
		self.today = getdate(nowdate())
		data = self.get_data()
		return (
			self.get_columns(),
			data,
			None,
			self.get_chart(data),
			self.get_report_summary(data),
		)

	def get_data(self):
		log = frappe.qb.DocType("Cycle Count Log")
		program = frappe.qb.DocType("Cycle Count Program")

		query = (
			frappe.qb.from_(log)
			.inner_join(program)
			.on(log.program == program.name)
			.select(
				log.name.as_("log"),
				log.program,
				program.company,
				log.item_code,
				log.warehouse,
				log.abc_class,
				log.velocity_value,
				log.last_counted_on,
				log.next_due_on,
				log.status,
				log.stock_reconciliation,
			)
			.orderby(log.next_due_on)
			.orderby(log.item_code)
		)

		if self.filters.get("company"):
			query = query.where(program.company == self.filters.company)
		if self.filters.get("program"):
			query = query.where(log.program == self.filters.program)
		if self.filters.get("abc_class"):
			query = query.where(log.abc_class == self.filters.abc_class)
		if self.filters.get("status"):
			query = query.where(log.status == self.filters.status)

		data = []
		for row in query.run(as_dict=True):
			days_overdue = 0
			if row.next_due_on and getdate(row.next_due_on) < self.today:
				days_overdue = date_diff(self.today, row.next_due_on)

			if cint(self.filters.only_overdue) and not days_overdue:
				continue

			row.days_overdue = days_overdue
			row.velocity_value = flt(row.velocity_value)
			data.append(row)

		return data

	def get_columns(self):
		return [
			{
				"fieldname": "program",
				"label": _("Program"),
				"fieldtype": "Link",
				"options": "Cycle Count Program",
				"width": 160,
			},
			{
				"fieldname": "item_code",
				"label": _("Item"),
				"fieldtype": "Link",
				"options": "Item",
				"width": 150,
			},
			{
				"fieldname": "warehouse",
				"label": _("Warehouse"),
				"fieldtype": "Link",
				"options": "Warehouse",
				"width": 140,
			},
			{
				"fieldname": "abc_class",
				"label": _("Class"),
				"fieldtype": "Data",
				"width": 70,
			},
			{
				"fieldname": "velocity_value",
				"label": _("Velocity Value"),
				"fieldtype": "Currency",
				"width": 120,
			},
			{
				"fieldname": "last_counted_on",
				"label": _("Last Counted"),
				"fieldtype": "Date",
				"width": 110,
			},
			{
				"fieldname": "next_due_on",
				"label": _("Next Due"),
				"fieldtype": "Date",
				"width": 110,
			},
			{
				"fieldname": "days_overdue",
				"label": _("Days Overdue"),
				"fieldtype": "Int",
				"width": 100,
			},
			{
				"fieldname": "status",
				"label": _("Status"),
				"fieldtype": "Data",
				"width": 100,
			},
			{
				"fieldname": "stock_reconciliation",
				"label": _("Count Sheet"),
				"fieldtype": "Link",
				"options": "Stock Reconciliation",
				"width": 160,
			},
		]

	def get_chart(self, data):
		counts = {"A": 0, "B": 0, "C": 0}
		for row in data:
			if row.abc_class in counts:
				counts[row.abc_class] += 1

		return {
			"data": {
				"labels": [_("Class A"), _("Class B"), _("Class C")],
				"datasets": [{"name": _("Items"), "values": list(counts.values())}],
			},
			"type": "donut",
			"height": 300,
			"colors": ["#ff5858", "#ffa00a", "#28a745"],
			"title": _("Items by ABC Class"),
		}

	def get_report_summary(self, data):
		total = len(data)
		overdue = sum(1 for row in data if row.days_overdue)
		on_time_pct = round(100.0 * (total - overdue) / total, 1) if total else 100.0

		return [
			{
				"value": on_time_pct,
				"indicator": "Green" if on_time_pct >= 95 else ("Orange" if on_time_pct >= 80 else "Red"),
				"label": _("% Counted On Time"),
				"datatype": "Percent",
			},
			{
				"value": overdue,
				"indicator": "Red" if overdue else "Green",
				"label": _("Items Overdue"),
				"datatype": "Int",
			},
			{
				"value": total,
				"indicator": "Blue",
				"label": _("Items Tracked"),
				"datatype": "Int",
			},
		]
