# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Order Promise Review - re-promise report for open Sales Orders.

For every open submitted Sales Order item due inside the filter window, the
ATP promise is recomputed from today's supply picture (Bin on-hand + open
POs/WOs, netted against all *other* committed SO demand). Rows whose
recomputed promise lands after the committed delivery date are flagged
"At Risk"; rows the horizon's supply cannot cover at all are "Shortfall".

Computation is capped at :data:`MAX_ROWS` Sales Order items (the promise is
recomputed per row, so the run is O(rows x supply events)); a message is
shown when the cap truncates the result.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import IfNull
from frappe.utils import cint, date_diff, flt, getdate, nowdate

from erpnext.stock.promising import loaders
from erpnext.stock.promising.engine import DEFAULT_HORIZON_DAYS, promise
from erpnext.stock.promising.models import DemandEvent, PromiseRequest

MAX_ROWS = 500

STATUS_OK = "OK"
STATUS_AT_RISK = "At Risk"
STATUS_SHORTFALL = "Shortfall"


def execute(filters=None):
	return OrderPromiseReview(filters).run()


class OrderPromiseReview:
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})

	def run(self):
		self.today = getdate(nowdate())
		self.horizon_end = loaders.get_horizon_end(
			self.today, cint(self.filters.horizon_days) or DEFAULT_HORIZON_DAYS
		)

		data = self.get_data()
		return self.get_columns(), data, None, self.get_chart(data)

	def get_data(self):
		so_items = self.get_open_so_items()
		if len(so_items) > MAX_ROWS:
			so_items = so_items[:MAX_ROWS]
			frappe.msgprint(
				_("Showing the first {0} open Sales Order items; narrow the filters to see the rest.").format(
					MAX_ROWS
				)
			)

		supply_cache = {}
		demand_cache = {}
		data = []

		for row in so_items:
			key = (row.item_code, row.warehouse)
			if key not in supply_cache:
				supply_cache[key] = loaders.get_supply_events(
					row.item_code, row.warehouse, self.filters.company
				)
				demand_cache[key] = loaders.get_committed_demand(
					row.item_code, row.warehouse, self.filters.company
				)

			# exclude the row's own Sales Order so it does not compete with itself
			other_demand = [
				DemandEvent(event.required_date, event.qty, event.reference)
				for event in demand_cache[key]
				if event.reference != row.sales_order
			]

			result = promise(
				PromiseRequest(
					item_code=row.item_code,
					warehouse=row.warehouse,
					qty=flt(row.pending_qty),
					requested_date=None,
				),
				supply_cache[key],
				other_demand,
				horizon_end=self.horizon_end,
				today=self.today,
			)

			committed_date = getdate(row.committed_date)
			if not result.fulfillable:
				status = STATUS_SHORTFALL
				slip_days = None
			elif result.promised_date > committed_date:
				status = STATUS_AT_RISK
				slip_days = date_diff(result.promised_date, committed_date)
			else:
				status = STATUS_OK
				slip_days = 0

			if cint(self.filters.only_at_risk) and status == STATUS_OK:
				continue

			data.append(
				{
					"sales_order": row.sales_order,
					"customer": row.customer,
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"pending_qty": flt(row.pending_qty),
					"committed_date": committed_date,
					"promised_date": result.promised_date,
					"slip_days": slip_days,
					"status": status,
					"message": result.message,
				}
			)

		return data

	def get_open_so_items(self):
		so = frappe.qb.DocType("Sales Order")
		so_item = frappe.qb.DocType("Sales Order Item")

		query = (
			frappe.qb.from_(so)
			.inner_join(so_item)
			.on(so.name == so_item.parent)
			.select(
				so.name.as_("sales_order"),
				so.customer,
				so_item.item_code,
				so_item.warehouse,
				((so_item.qty - so_item.delivered_qty) * so_item.conversion_factor).as_("pending_qty"),
				IfNull(so_item.delivery_date, so.delivery_date).as_("committed_date"),
			)
			.where(
				(so.docstatus == 1)
				& (so.status.notin(["Closed"]))
				& (so.company == self.filters.company)
				& (so_item.qty > so_item.delivered_qty)
			)
			.orderby(so_item.delivery_date)
			.orderby(so.name)
		)

		if self.filters.get("from_date"):
			query = query.where(IfNull(so_item.delivery_date, so.delivery_date) >= self.filters.from_date)
		if self.filters.get("to_date"):
			query = query.where(IfNull(so_item.delivery_date, so.delivery_date) <= self.filters.to_date)
		if self.filters.get("warehouse"):
			query = query.where(so_item.warehouse == self.filters.warehouse)
		if self.filters.get("item_code"):
			query = query.where(so_item.item_code == self.filters.item_code)

		return query.run(as_dict=True)

	def get_columns(self):
		return [
			{
				"fieldname": "sales_order",
				"label": _("Sales Order"),
				"fieldtype": "Link",
				"options": "Sales Order",
				"width": 160,
			},
			{
				"fieldname": "customer",
				"label": _("Customer"),
				"fieldtype": "Link",
				"options": "Customer",
				"width": 150,
			},
			{
				"fieldname": "item_code",
				"label": _("Item"),
				"fieldtype": "Link",
				"options": "Item",
				"width": 140,
			},
			{
				"fieldname": "warehouse",
				"label": _("Warehouse"),
				"fieldtype": "Link",
				"options": "Warehouse",
				"width": 140,
			},
			{
				"fieldname": "pending_qty",
				"label": _("Qty Pending"),
				"fieldtype": "Float",
				"width": 100,
			},
			{
				"fieldname": "committed_date",
				"label": _("Committed Date"),
				"fieldtype": "Date",
				"width": 120,
			},
			{
				"fieldname": "promised_date",
				"label": _("Recomputed Promise Date"),
				"fieldtype": "Date",
				"width": 130,
			},
			{
				"fieldname": "slip_days",
				"label": _("Slip (Days)"),
				"fieldtype": "Int",
				"width": 90,
			},
			{
				"fieldname": "status",
				"label": _("Status"),
				"fieldtype": "Data",
				"width": 100,
			},
			{
				"fieldname": "message",
				"label": _("Message"),
				"fieldtype": "Data",
				"width": 280,
			},
		]

	def get_chart(self, data):
		counts = {STATUS_OK: 0, STATUS_AT_RISK: 0, STATUS_SHORTFALL: 0}
		for row in data:
			counts[row["status"]] += 1

		return {
			"data": {
				"labels": [_(STATUS_OK), _(STATUS_AT_RISK), _(STATUS_SHORTFALL)],
				"datasets": [{"name": _("Sales Order Items"), "values": list(counts.values())}],
			},
			"type": "donut",
			"height": 300,
			"colors": ["#28a745", "#ffa00a", "#ff5858"],
			"title": _("Promise Status"),
		}
