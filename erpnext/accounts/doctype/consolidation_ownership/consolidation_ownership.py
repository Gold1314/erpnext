# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate


class ConsolidationOwnership(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		consolidation_method: DF.Literal["Full", "Equity"]
		effective_from: DF.Date | None
		effective_to: DF.Date | None
		ownership_percent: DF.Percent
		parent_company: DF.Link
		subsidiary: DF.Link
	# end: auto-generated types

	def validate(self):
		self.validate_companies()
		self.validate_percent()
		self.validate_dates()
		self.validate_no_overlap()

	def validate_companies(self):
		if self.subsidiary == self.parent_company:
			frappe.throw(_("Subsidiary cannot be the same company as the Parent Company."))

	def validate_percent(self):
		percent = flt(self.ownership_percent)
		if percent <= 0 or percent > 100:
			frappe.throw(_("Ownership Percent must be greater than 0 and at most 100."))

	def validate_dates(self):
		if (
			self.effective_from
			and self.effective_to
			and getdate(self.effective_from) > getdate(self.effective_to)
		):
			frappe.throw(_("Effective From cannot be after Effective To."))

	def validate_no_overlap(self):
		"""No two records for the same parent/subsidiary pair may have
		overlapping effective periods (open-ended periods overlap everything
		beyond their start)."""
		others = frappe.get_all(
			"Consolidation Ownership",
			filters={
				"parent_company": self.parent_company,
				"subsidiary": self.subsidiary,
				"name": ("!=", self.name or ""),
			},
			fields=["name", "effective_from", "effective_to"],
		)

		own_from = getdate(self.effective_from) if self.effective_from else None
		own_to = getdate(self.effective_to) if self.effective_to else None

		for other in others:
			other_from = getdate(other.effective_from) if other.effective_from else None
			other_to = getdate(other.effective_to) if other.effective_to else None

			# two half-open intervals do NOT overlap only when one ends
			# strictly before the other begins
			if (own_to and other_from and own_to < other_from) or (
				other_to and own_from and other_to < own_from
			):
				continue

			frappe.throw(
				_(
					"The effective period overlaps with {0} for the same parent/subsidiary pair ({1} -> {2})."
				).format(other.name, self.parent_company, self.subsidiary)
			)
