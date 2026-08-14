# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class InboundEInvoiceItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		buyer_item_code: DF.Data | None
		description: DF.SmallText | None
		expense_account: DF.Link | None
		item_code: DF.Link | None
		item_name: DF.Data | None
		line_id: DF.Data | None
		line_net: DF.Currency
		match_method: DF.Data | None
		matched_po: DF.Link | None
		matched_po_detail: DF.Data | None
		order_line_ref: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		price_variance: DF.Percent
		qty: DF.Float
		qty_variance: DF.Percent
		seller_item_code: DF.Data | None
		tax_rate: DF.Percent
		unit_price: DF.Currency
		uom: DF.Link | None
		within_tolerance: DF.Check
	# end: auto-generated types

	pass
