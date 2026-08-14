# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AIDocumentExtractionTax(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_head: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		rate_pct: DF.Percent
		tax_amount: DF.Currency
	# end: auto-generated types

	pass
