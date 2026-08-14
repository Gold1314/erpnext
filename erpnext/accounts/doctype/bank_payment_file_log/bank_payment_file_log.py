# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BankPaymentFileLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		control_sum: DF.Currency
		currency: DF.Link | None
		file_url: DF.Data | None
		generated_on: DF.Datetime | None
		number_of_transactions: DF.Int
		pain_variant: DF.Data | None
		payment_order: DF.Link
		profile: DF.Link | None
		remarks: DF.SmallText | None
		status: DF.Literal["Generated", "Transmitted", "Failed"]
		transmitted_on: DF.Datetime | None
		warnings: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		if self.status == "Transmitted" and not self.transmitted_on:
			self.transmitted_on = frappe.utils.now_datetime()
		if self.status != "Transmitted":
			self.transmitted_on = None

	@frappe.whitelist()
	def mark_failed(self, remarks: str | None = None):
		"""Record that the bank rejected this file (or the upload failed)."""
		self.status = "Failed"
		if remarks:
			self.remarks = remarks
		self.save()
		frappe.msgprint(
			_("Bank Payment File Log {0} marked as Failed").format(self.name), alert=True
		)
