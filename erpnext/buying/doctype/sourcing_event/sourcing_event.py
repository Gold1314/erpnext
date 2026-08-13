# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, nowdate

from erpnext.buying.doctype.supplier_qualification.supplier_qualification import (
	get_supplier_qualification,
)
from erpnext.buying.sourcing.engine import build_award_scenarios, score_bids
from erpnext.buying.sourcing.models import (
	W_LEAD_TIME,
	W_PRICE,
	W_QUALIFICATION,
	W_SCORECARD,
	BidLine,
)

STATUS_DRAFT = "Draft"
STATUS_OPEN = "Open"
STATUS_CLOSED = "Closed"
STATUS_AWARDED = "Awarded"
STATUS_CANCELLED = "Cancelled"

#: statuses in which bid values may be revealed for comparison
UNSEALED_STATUSES = (STATUS_CLOSED, STATUS_AWARDED)


class SourcingEvent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.buying.doctype.sourcing_event_item.sourcing_event_item import SourcingEventItem
		from erpnext.buying.doctype.sourcing_event_supplier.sourcing_event_supplier import (
			SourcingEventSupplier,
		)

		amended_from: DF.Link | None
		close_date: DF.Date
		company: DF.Link
		event_type: DF.Literal["RFQ", "RFP", "Reverse Auction"]
		items: DF.Table[SourcingEventItem]
		notes: DF.SmallText | None
		open_date: DF.Date | None
		require_qualification: DF.Check
		sealed_until_close: DF.Check
		status: DF.Literal["Draft", "Open", "Closed", "Awarded", "Cancelled"]
		suppliers: DF.Table[SourcingEventSupplier]
		title: DF.Data
		weight_lead_time: DF.Float
		weight_price: DF.Float
		weight_qualification: DF.Float
		weight_scorecard: DF.Float
	# end: auto-generated types

	def validate(self):
		self.validate_dates()
		self.validate_weights()
		self.validate_suppliers()
		self.validate_items()
		self.set_qualification_status()

		if self.docstatus == 0:
			self.status = STATUS_DRAFT

	def validate_dates(self):
		if self.open_date and getdate(self.open_date) > getdate(self.close_date):
			frappe.throw(_("Close Date cannot be before the Open Date."))

	def validate_weights(self):
		for fieldname in (
			"weight_price",
			"weight_lead_time",
			"weight_scorecard",
			"weight_qualification",
		):
			if flt(self.get(fieldname)) < 0:
				frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(fieldname))))

		if self.total_weight() <= 0:
			frappe.throw(_("At least one award weight must be greater than zero."))

	def validate_suppliers(self):
		seen = set()
		for row in self.suppliers:
			if row.supplier in seen:
				frappe.throw(_("Supplier {0} is listed more than once.").format(row.supplier))
			seen.add(row.supplier)

	def validate_items(self):
		seen = set()
		for row in self.items:
			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Quantity must be greater than zero.").format(row.idx))
			if row.item_code in seen:
				frappe.throw(_("Item {0} is listed more than once.").format(row.item_code))
			seen.add(row.item_code)

	def set_qualification_status(self):
		"""Stamp each invited supplier's current qualification standing."""
		for row in self.suppliers:
			qualification = get_supplier_qualification(row.supplier)
			if not qualification:
				row.qualification_status = _("Not Qualified")
			else:
				row.qualification_status = "{} ({}%)".format(
					_(qualification.get("status")), flt(qualification.get("score_percent"), 1)
				)

	def total_weight(self) -> float:
		return (
			flt(self.weight_price)
			+ flt(self.weight_lead_time)
			+ flt(self.weight_scorecard)
			+ flt(self.weight_qualification)
		)

	def weights(self) -> dict[str, float]:
		return {
			W_PRICE: flt(self.weight_price),
			W_LEAD_TIME: flt(self.weight_lead_time),
			W_SCORECARD: flt(self.weight_scorecard),
			W_QUALIFICATION: flt(self.weight_qualification),
		}

	# -- lifecycle -------------------------------------------------------
	def on_submit(self):
		self.db_set("status", STATUS_OPEN)

	def on_cancel(self):
		if frappe.db.exists("Sourcing Event Bid", {"sourcing_event": self.name, "docstatus": 1}):
			frappe.throw(_("Cancel the submitted bids on this event before cancelling the event itself."))
		self.db_set("status", STATUS_CANCELLED)

	def accepts_bids(self, as_of=None) -> bool:
		"""True while suppliers may still submit bids."""
		as_of = getdate(as_of or nowdate())
		return self.docstatus == 1 and self.status == STATUS_OPEN and getdate(self.close_date) >= as_of

	# -- actions ---------------------------------------------------------
	@frappe.whitelist()
	def close_event(self, force: bool = False):
		"""Close bidding.

		Normally only allowed on or after ``close_date``; closing early
		(``force``) is restricted to Purchase Manager / System Manager so a
		buyer cannot cut a sealed event short on their own.
		"""
		if self.docstatus != 1:
			frappe.throw(_("Only a submitted Sourcing Event can be closed."))
		if self.status in UNSEALED_STATUSES:
			frappe.throw(_("Sourcing Event {0} is already {1}.").format(self.name, _(self.status)))
		if self.status == STATUS_CANCELLED:
			frappe.throw(_("A cancelled Sourcing Event cannot be closed."))

		if getdate(nowdate()) < getdate(self.close_date):
			if not cint(force):
				frappe.throw(
					_("This event closes on {0}. Use Force Close to close it early.").format(
						frappe.format(self.close_date, {"fieldtype": "Date"})
					)
				)
			frappe.only_for(("Purchase Manager", "System Manager"))

		self.db_set("status", STATUS_CLOSED)
		return self.status

	@frappe.whitelist()
	def get_bids_for_comparison(self) -> list[dict]:
		"""Submitted bid lines, honoring the sealed-bid rule.

		While ``sealed_until_close`` is set and the event is not yet Closed
		(or Awarded), no bid values are revealed - not to the buyer, not to
		the reports, not to the award engine.
		"""
		if self.sealed_until_close and self.status not in UNSEALED_STATUSES:
			frappe.throw(
				_("Bids on {0} are sealed until the event is closed.").format(self.name),
				title=_("Sealed Bids"),
			)

		bid = frappe.qb.DocType("Sourcing Event Bid")
		item = frappe.qb.DocType("Sourcing Event Bid Item")

		return (
			frappe.qb.from_(item)
			.join(bid)
			.on(item.parent == bid.name)
			.select(
				bid.name.as_("bid"),
				bid.supplier,
				bid.currency,
				bid.bid_date,
				bid.valid_until,
				item.item_code,
				item.qty,
				item.unit_price,
				item.amount,
				item.lead_time_days,
				item.remarks,
			)
			.where((bid.sourcing_event == self.name) & (bid.docstatus == 1))
			.orderby(bid.supplier)
			.orderby(item.item_code)
			.run(as_dict=True)
		)

	@frappe.whitelist()
	def compute_award_analysis(self) -> dict:
		"""Score the submitted bids and build the three award scenarios.

		**Nothing is cached on the document** - the analysis is a pure
		function of the bids, the weights and the suppliers' current
		scorecard / qualification standing, all of which move independently
		of this doc. Caching would go stale silently and would also leak
		sealed values into a stored field. The report
		``Sourcing Event Award Analysis`` calls this same method live.
		"""
		lines = self.get_bids_for_comparison()

		scorecards = {}
		qualifications = {}
		excluded = []
		suppliers = sorted({row["supplier"] for row in lines})

		for supplier in suppliers:
			scorecards[supplier] = get_scorecard_score(supplier)
			qualification = get_supplier_qualification(supplier)
			if qualification and qualification.get("is_qualified"):
				qualifications[supplier] = flt(qualification.get("score_percent"))
			elif self.require_qualification:
				excluded.append(supplier)

		if self.require_qualification and excluded:
			lines = [row for row in lines if row["supplier"] not in excluded]

		bid_lines = [
			BidLine(
				supplier=row["supplier"],
				item_code=row["item_code"],
				qty=flt(row["qty"]),
				unit_price=flt(row["unit_price"]),
				lead_time_days=flt(row["lead_time_days"]),
			)
			for row in lines
		]

		scores = score_bids(bid_lines, self.weights(), scorecards, qualifications)
		scenarios = build_award_scenarios(bid_lines, scores, item_codes=[row.item_code for row in self.items])

		return {
			"event": self.name,
			"status": self.status,
			"currency": (lines[0]["currency"] if lines else None),
			"weights": self.weights(),
			"excluded_suppliers": excluded,
			"lines": lines,
			"scores": [
				{
					"supplier": score.supplier,
					"price_score": score.price_score,
					"lead_time_score": score.lead_time_score,
					"scorecard_score": score.scorecard_score,
					"qualification_score": score.qualification_score,
					"weighted_total": score.weighted_total,
					"rank": score.rank,
				}
				for score in scores
			],
			"scenarios": [
				{
					"name": scenario.name,
					"awards": [
						{"item_code": item_code, "supplier": supplier, "qty": qty, "price": price}
						for item_code, supplier, qty, price in scenario.awards
					],
					"total_cost": scenario.total_cost,
					"coverage_gaps": scenario.coverage_gaps,
				}
				for scenario in scenarios
			],
		}

	@frappe.whitelist()
	def award_to(self, supplier: str, item_codes=None) -> str:
		"""Create a draft Supplier Quotation from the winning supplier's bid.

		``item_codes`` (JSON list or comma-separated string) restricts the
		award to specific lines so an event can be split across suppliers -
		call ``award_to`` once per winner. The event moves to Awarded; the
		Supplier Quotation is left in **draft** so the buyer reviews taxes,
		terms and pricing before submitting (same posture as the portal
		mapper ``create_supplier_quotation``).
		"""
		if self.docstatus != 1:
			frappe.throw(_("Only a submitted Sourcing Event can be awarded."))
		if self.status not in UNSEALED_STATUSES:
			frappe.throw(_("Close the event before awarding it."))

		item_codes = parse_item_codes(item_codes)

		bid_name = frappe.db.get_value(
			"Sourcing Event Bid",
			{"sourcing_event": self.name, "supplier": supplier, "docstatus": 1},
			"name",
		)
		if not bid_name:
			frappe.throw(_("Supplier {0} has no submitted bid on this event.").format(supplier))

		bid = frappe.get_doc("Sourcing Event Bid", bid_name)
		event_items = {row.item_code: row for row in self.items}

		lines = [
			row
			for row in bid.items
			if (not item_codes or row.item_code in item_codes) and flt(row.unit_price) > 0
		]
		if not lines:
			frappe.throw(_("No priced bid lines to award for supplier {0}.").format(supplier))

		quotation = frappe.new_doc("Supplier Quotation")
		quotation.supplier = supplier
		quotation.company = self.company
		quotation.transaction_date = nowdate()
		if bid.currency:
			quotation.currency = bid.currency
		if bid.valid_until:
			quotation.valid_till = bid.valid_until

		for row in lines:
			event_row = event_items.get(row.item_code)
			quotation.append(
				"items",
				{
					"item_code": row.item_code,
					"item_name": event_row.item_name if event_row else None,
					"description": event_row.description if event_row else None,
					"qty": flt(row.qty),
					"uom": event_row.uom if event_row else None,
					"rate": flt(row.unit_price),
					"lead_time_days": cint(row.lead_time_days),
					"expected_delivery_date": event_row.schedule_date if event_row else None,
				},
			)

		quotation.run_method("set_missing_values")
		quotation.insert()

		self.db_set("status", STATUS_AWARDED)
		frappe.msgprint(
			_("Supplier Quotation {0} created as a draft.").format(
				frappe.utils.get_link_to_form("Supplier Quotation", quotation.name)
			)
		)
		return quotation.name


def parse_item_codes(item_codes) -> list[str]:
	if not item_codes:
		return []
	if isinstance(item_codes, str):
		try:
			item_codes = frappe.parse_json(item_codes)
		except Exception:
			item_codes = [code.strip() for code in item_codes.split(",")]
	if isinstance(item_codes, str):
		item_codes = [item_codes]
	return [code for code in item_codes if code]


def get_scorecard_score(supplier: str) -> float:
	"""Supplier Scorecard standing on a 0-100 scale.

	``Supplier Scorecard`` is named after the supplier (``autoname:
	field:supplier``) and stores its weighted standing in the ``supplier_score``
	Data field (see ``supplier_scorecard.calculate_total_score``). Suppliers
	without a scorecard return 0 - they are still ranked, they simply earn no
	scorecard points.
	"""
	score = frappe.db.get_value("Supplier Scorecard", {"supplier": supplier}, "supplier_score")
	return flt(score)
