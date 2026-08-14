# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SupplierQualificationTemplateItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		category: DF.Literal["Financial", "Quality", "Compliance", "Capability", "Sustainability"]
		guidance: DF.SmallText | None
		is_knockout: DF.Check
		max_score: DF.Float
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		question: DF.SmallText
		weight: DF.Float
	# end: auto-generated types

	pass
