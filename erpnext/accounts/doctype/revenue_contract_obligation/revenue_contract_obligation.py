# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RevenueContractObligation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allocated_amount: DF.Currency
		allocation_pct: DF.Percent
		description: DF.Data
		income_account: DF.Link
		item_code: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		recognized_amount: DF.Currency
		satisfaction_method: DF.Literal["Point in Time", "Over Time"]
		satisfied: DF.Check
		satisfied_date: DF.Date | None
		service_end_date: DF.Date | None
		service_start_date: DF.Date | None
		ssp: DF.Currency
		stated_amount: DF.Currency
	# end: auto-generated types

	pass
