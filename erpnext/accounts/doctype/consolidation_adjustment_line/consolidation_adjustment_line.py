# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ConsolidationAdjustmentLine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account: DF.Data | None
		company: DF.Link | None
		credit: DF.Currency
		debit: DF.Currency
		line_type: DF.Literal["Elimination", "Minority Interest", "CTA"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		rule: DF.Data | None
	# end: auto-generated types

	pass
