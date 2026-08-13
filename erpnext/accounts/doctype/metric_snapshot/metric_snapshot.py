# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from erpnext.analytics import registry


class MetricSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		computed_on: DF.Datetime | None
		inputs_json: DF.LongText | None
		label: DF.Data | None
		metric_key: DF.Data
		period_end: DF.Date | None
		period_start: DF.Date | None
		status: DF.Data | None
		unit: DF.Data | None
		value: DF.Float
		warnings: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		self.validate_period()
		self.sync_from_registry()

	def validate_period(self):
		if self.period_start and self.period_end and getdate(self.period_start) > getdate(self.period_end):
			frappe.throw(_("Period Start cannot be after Period End."))

	def sync_from_registry(self):
		"""Label and unit always describe the catalog entry, never free text."""
		spec = registry.get_spec(self.metric_key or "")
		if not spec:
			return
		self.label = spec.label
		self.unit = spec.unit
