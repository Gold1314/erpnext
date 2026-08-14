# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CycleCountLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		abc_class: DF.Literal["", "A", "B", "C"]
		item_code: DF.Link
		last_counted_on: DF.Date | None
		next_due_on: DF.Date | None
		program: DF.Link
		status: DF.Literal["Classified", "Scheduled", "Counted"]
		stock_reconciliation: DF.Link | None
		velocity_value: DF.Currency
		warehouse: DF.Link | None
	# end: auto-generated types

	pass
