# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RevenueRecognitionEntry(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		journal_entry: DF.Link | None
		obligation_description: DF.Data | None
		obligation_idx: DF.Int
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		period_end: DF.Date | None
		period_start: DF.Date | None
		posted: DF.Check
	# end: auto-generated types

	pass
