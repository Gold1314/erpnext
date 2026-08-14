# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AgentPolicyRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allow: DF.Check
		doctype_pattern: DF.Data
		max_amount: DF.Currency
		notes: DF.SmallText | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		risk_level: DF.Literal["READ", "DRAFT_WRITE", "SUBMIT", "DESTRUCTIVE"]
		role: DF.Link | None
	# end: auto-generated types

	pass
