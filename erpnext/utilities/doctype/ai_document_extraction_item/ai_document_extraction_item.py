# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AIDocumentExtractionItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		description: DF.SmallText | None
		expense_account: DF.Link | None
		item_code: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		qty: DF.Float
		tax_rate: DF.Percent
		unit_price: DF.Currency
		uom: DF.Link | None
	# end: auto-generated types

	pass
