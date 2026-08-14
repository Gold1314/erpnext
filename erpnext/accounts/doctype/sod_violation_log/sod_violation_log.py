# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SoDViolationLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		detected_on: DF.Datetime | None
		first_function_roles: DF.SmallText | None
		mitigation_notes: DF.Text | None
		resolved_by: DF.Link | None
		resolved_on: DF.Datetime | None
		risk_level: DF.Literal["High", "Medium", "Low"]
		second_function_roles: DF.SmallText | None
		sod_rule: DF.Link
		status: DF.Literal["Open", "Mitigated", "Accepted", "Resolved"]
		user: DF.Link
	# end: auto-generated types

	def validate(self):
		self.set_resolution_details()

	def set_resolution_details(self):
		if self.status == "Open":
			self.resolved_by = None
			self.resolved_on = None
		elif not self.resolved_on:
			self.resolved_by = self.resolved_by or frappe.session.user
			self.resolved_on = now_datetime()
