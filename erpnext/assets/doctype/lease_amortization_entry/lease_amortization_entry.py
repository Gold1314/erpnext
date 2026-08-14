# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class LeaseAmortizationEntry(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		closing_liability: DF.Currency
		closing_rou: DF.Currency
		interest: DF.Currency
		journal_entry: DF.Link | None
		opening_liability: DF.Currency
		opening_rou: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payment: DF.Currency
		period_end: DF.Date | None
		period_start: DF.Date | None
		posted: DF.Check
		principal: DF.Currency
		rou_depreciation: DF.Currency
	# end: auto-generated types

	pass
