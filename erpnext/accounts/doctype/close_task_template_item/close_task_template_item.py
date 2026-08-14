# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CloseTaskTemplateItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_verify: DF.Check
		depends_on_titles: DF.Data | None
		due_day_offset: DF.Int
		instructions: DF.SmallText | None
		owner_role: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		task_title: DF.Data
		task_type: DF.Literal[
			"Manual",
			"Period Closing Voucher",
			"Exchange Rate Revaluation",
			"Deferred Accounting",
			"Bank Reconciliation",
			"Ledger Health",
			"Accounting Period Lock",
		]
	# end: auto-generated types

	pass
