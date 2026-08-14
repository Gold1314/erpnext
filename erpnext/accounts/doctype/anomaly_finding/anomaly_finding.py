# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

#: statuses that block re-insertion of the same fingerprint (mirrors
#: erpnext.accounts.anomaly.loaders.BLOCKING_STATUSES)
BLOCKING_STATUSES = ("Open", "Investigating", "Confirmed Issue", "False Positive")

#: statuses that count as closure and get resolved_by/resolved_on stamped
CLOSED_STATUSES = ("Resolved", "False Positive")


class AnomalyFinding(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		check_key: DF.Data
		company: DF.Link
		details: DF.LongText | None
		detected_on: DF.Datetime | None
		entity_name: DF.Data | None
		entity_type: DF.Data | None
		fingerprint: DF.Data | None
		message: DF.SmallText | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		resolution_notes: DF.Text | None
		resolved_by: DF.Link | None
		resolved_on: DF.Datetime | None
		score: DF.Float
		severity: DF.Literal["High", "Medium", "Low"]
		status: DF.Literal["Open", "Investigating", "Confirmed Issue", "False Positive", "Resolved"]
	# end: auto-generated types

	def validate(self):
		self.set_resolution_details()

	def before_insert(self):
		self.check_duplicate_fingerprint()

	def set_resolution_details(self):
		"""Stamp/clear resolution metadata from the status transition
		(pattern: erpnext/accounts/doctype/sod_violation_log/sod_violation_log.py)."""
		if self.status in CLOSED_STATUSES:
			if not self.resolved_on:
				self.resolved_by = self.resolved_by or frappe.session.user
				self.resolved_on = now_datetime()
		else:
			self.resolved_by = None
			self.resolved_on = None

	def check_duplicate_fingerprint(self):
		"""Guard against inserting an exact duplicate of a finding that is
		still open (or was dismissed as a false positive). Resolved findings
		may legitimately recur, so they do not block."""
		if not self.fingerprint:
			return

		existing = frappe.db.get_value(
			"Anomaly Finding",
			{"fingerprint": self.fingerprint, "status": ("in", BLOCKING_STATUSES)},
			"name",
		)
		if existing:
			frappe.throw(
				_("An unresolved Anomaly Finding with the same fingerprint already exists: {0}").format(
					existing
				),
				title=_("Duplicate Finding"),
			)
