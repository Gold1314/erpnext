# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SourcingEventSupplier(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		contact_email: DF.Data | None
		invited: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		qualification_status: DF.Data | None
		responded: DF.Check
		supplier: DF.Link
		supplier_name: DF.ReadOnly | None
	# end: auto-generated types

	pass
