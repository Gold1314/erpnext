# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class SSPPrice(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		basis: DF.Literal["Observable", "Adjusted Market", "Expected Cost Plus", "Residual"]
		company: DF.Link | None
		currency: DF.Link | None
		item_code: DF.Link
		notes: DF.SmallText | None
		ssp_rate: DF.Currency
		valid_from: DF.Date | None
	# end: auto-generated types

	def validate(self):
		if self.ssp_rate < 0:
			frappe.throw(_("Standalone Selling Price cannot be negative."))
		self.validate_duplicate()

	def validate_duplicate(self):
		"""One SSP record per item / company / valid_from.

		``get_ssp`` resolves the record with the latest ``valid_from`` on or
		before the contract date, so two records on the same key would make
		the winner ambiguous.
		"""
		duplicate = frappe.db.exists(
			"SSP Price",
			{
				"name": ("!=", self.name),
				"item_code": self.item_code,
				"company": self.company or "",
				"valid_from": self.valid_from or "",
			},
		)
		if duplicate:
			frappe.throw(
				_("SSP Price {0} already exists for Item {1} with the same Company and Valid From.").format(
					duplicate, self.item_code
				),
				title=_("Duplicate SSP Price"),
			)


def get_ssp(item_code: str, company: str | None = None, date=None) -> float | None:
	"""Resolve the standalone selling price for an item as of a date.

	Selection: records with ``valid_from`` on or before ``date`` (or no
	``valid_from`` at all), latest ``valid_from`` wins. A company-specific
	record beats a company-agnostic one. Returns ``None`` when no SSP is on
	file (callers fall back to the stated amount and say so).
	"""
	date = getdate(date) if date else getdate()

	def _lookup(filters):
		rows = frappe.get_all(
			"SSP Price",
			filters=filters,
			or_filters=[["valid_from", "is", "not set"], ["valid_from", "<=", date]],
			fields=["ssp_rate", "valid_from"],
			order_by="valid_from desc",
			limit=1,
		)
		return rows[0].ssp_rate if rows else None

	if company:
		rate = _lookup({"item_code": item_code, "company": company})
		if rate is not None:
			return rate

	return _lookup({"item_code": item_code, "company": ("in", ("", None))})
