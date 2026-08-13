# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

CLOSED_EVENT_STATUSES = ("Closed", "Awarded", "Cancelled")


class SourcingEventBid(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.buying.doctype.sourcing_event_bid_item.sourcing_event_bid_item import (
			SourcingEventBidItem,
		)

		amended_from: DF.Link | None
		bid_date: DF.Date | None
		currency: DF.Link | None
		items: DF.Table[SourcingEventBidItem]
		notes: DF.SmallText | None
		sourcing_event: DF.Link
		supplier: DF.Link
		supplier_name: DF.Data | None
		total_amount: DF.Currency
		valid_until: DF.Date | None
	# end: auto-generated types

	def validate(self):
		self.validate_event_open()
		self.validate_invited_supplier()
		self.validate_items()
		self.validate_duplicate_bid()
		self.calculate_totals()

	def get_event(self):
		return frappe.get_doc("Sourcing Event", self.sourcing_event)

	def validate_event_open(self):
		event = self.get_event()

		if event.docstatus != 1:
			frappe.throw(_("Sourcing Event {0} is not open for bidding.").format(event.name))

		if event.status in CLOSED_EVENT_STATUSES:
			frappe.throw(
				_("Sourcing Event {0} is {1} - bids are no longer accepted.").format(
					event.name, _(event.status)
				)
			)

		bid_date = getdate(self.bid_date or nowdate())
		if getdate(event.close_date) < bid_date:
			frappe.throw(
				_("Sourcing Event {0} closed on {1}. Late bids are not accepted.").format(
					event.name, frappe.format(event.close_date, {"fieldtype": "Date"})
				)
			)
		if event.open_date and bid_date < getdate(event.open_date):
			frappe.throw(
				_("Sourcing Event {0} opens on {1}.").format(
					event.name, frappe.format(event.open_date, {"fieldtype": "Date"})
				)
			)

	def validate_invited_supplier(self):
		event = self.get_event()
		invited = {row.supplier for row in event.suppliers if row.invited}
		if invited and self.supplier not in invited:
			frappe.throw(
				_("Supplier {0} was not invited to Sourcing Event {1}.").format(
					self.supplier, self.sourcing_event
				)
			)

	def validate_items(self):
		if not self.items:
			frappe.throw(_("Add at least one bid line."))

		event_items = {row.item_code for row in self.get_event().items}
		seen = set()

		for row in self.items:
			if row.item_code not in event_items:
				frappe.throw(
					_("Row {0}: Item {1} is not part of Sourcing Event {2}.").format(
						row.idx, row.item_code, self.sourcing_event
					)
				)
			if row.item_code in seen:
				frappe.throw(_("Item {0} is bid more than once.").format(row.item_code))
			seen.add(row.item_code)

			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Quantity must be greater than zero.").format(row.idx))
			if flt(row.unit_price) <= 0:
				frappe.throw(_("Row {0}: Unit Price must be greater than zero.").format(row.idx))

	def validate_duplicate_bid(self):
		existing = frappe.db.get_value(
			"Sourcing Event Bid",
			{
				"sourcing_event": self.sourcing_event,
				"supplier": self.supplier,
				"docstatus": 1,
				"name": ("!=", self.name),
			},
			"name",
		)
		if existing:
			frappe.throw(
				_("Supplier {0} already has a submitted bid ({1}) on this event. Cancel it first.").format(
					self.supplier, existing
				)
			)

	def calculate_totals(self):
		total = 0.0
		for row in self.items:
			row.amount = flt(row.qty) * flt(row.unit_price)
			total += flt(row.amount)
		self.total_amount = total

	def on_submit(self):
		self.mark_supplier_responded(1)

	def on_cancel(self):
		if not frappe.db.exists(
			"Sourcing Event Bid",
			{
				"sourcing_event": self.sourcing_event,
				"supplier": self.supplier,
				"docstatus": 1,
				"name": ("!=", self.name),
			},
		):
			self.mark_supplier_responded(0)

	def mark_supplier_responded(self, responded: int):
		row_name = frappe.db.get_value(
			"Sourcing Event Supplier",
			{"parent": self.sourcing_event, "parenttype": "Sourcing Event", "supplier": self.supplier},
			"name",
		)
		if row_name:
			frappe.db.set_value("Sourcing Event Supplier", row_name, "responded", responded)
