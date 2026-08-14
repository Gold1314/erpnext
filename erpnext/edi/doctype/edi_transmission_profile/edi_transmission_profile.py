# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext.edi.ubl.profiles import get_profile


class EDITransmissionProfile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		endpoint_scheme_seller: DF.Data | None
		is_default: DF.Check
		notes: DF.SmallText | None
		profile_name: DF.Data
		seller_endpoint_id: DF.Data | None
		transport_mode: DF.Literal["Manual", "API"]
		ubl_profile: DF.Literal["peppol-bis-3", "xrechnung-3", "en16931"]
	# end: auto-generated types

	def validate(self):
		self.validate_ubl_profile()
		self.validate_endpoint()
		self.enforce_single_default()

	def validate_ubl_profile(self):
		try:
			get_profile(self.ubl_profile)
		except ValueError as e:
			frappe.throw(str(e), title=_("Unknown UBL Profile"))

	def validate_endpoint(self):
		if self.seller_endpoint_id and not self.endpoint_scheme_seller:
			frappe.throw(
				_("Endpoint Scheme ID is required when a Seller Endpoint ID is set (e.g. 0088 for GLN)")
			)

	def enforce_single_default(self):
		"""Keep at most one default profile per company."""
		if not self.is_default:
			return

		others = frappe.get_all(
			"EDI Transmission Profile",
			filters={"company": self.company, "is_default": 1, "name": ("!=", self.name)},
			pluck="name",
		)
		for name in others:
			frappe.db.set_value("EDI Transmission Profile", name, "is_default", 0)
			frappe.msgprint(
				_("Removed default flag from EDI Transmission Profile {0}").format(name),
				alert=True,
			)
