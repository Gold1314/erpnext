# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EDITransmissionLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		file_url: DF.Data | None
		generated_on: DF.Datetime | None
		message: DF.SmallText | None
		profile: DF.Link | None
		sales_invoice: DF.Link
		status: DF.Literal["Generated", "Queued", "Transmitted", "Failed"]
		transmitted_on: DF.Datetime | None
		ubl_profile: DF.Data | None
		warnings: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		if self.status == "Transmitted" and not self.transmitted_on:
			self.transmitted_on = frappe.utils.now_datetime()

	@frappe.whitelist()
	def mark_failed(self, message: str | None = None):
		"""Convenience for adapters and users to record a failed attempt."""
		self.status = "Failed"
		if message:
			self.message = message
		self.save()
		frappe.msgprint(_("Log {0} marked as Failed").format(self.name), alert=True)
