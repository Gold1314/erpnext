# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Agent Action Log — one append-only row per agent tool call.

Permissions (see the JSON) make this an evidence table, not a working
document:

* role **All**: ``create`` + ``read`` with ``if_owner`` — the acting user
  (or the dedicated agent user) appends its own calls and can read them
  back. That is what lets ``audit.log_action`` write the row **without**
  ``ignore_permissions``, which appears nowhere in this feature.
* **System Manager / Auditor / Accounts Manager**: read, report, export
  across all users. System Manager may also delete, for retention policy.
* **No role has ``write`` or ``amend``.** A logged call cannot be edited by
  anyone, including the user who made it.

``in_create`` keeps the desk from offering a "new log entry" form: rows come
from the guarded tool path or not at all.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class AgentActionLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		decision: DF.Literal["", "Allowed", "Denied"]
		doctype_touched: DF.Data | None
		duration_ms: DF.Int
		error: DF.SmallText | None
		outcome: DF.Literal["", "Success", "Error", "Denied"]
		params_json: DF.LongText | None
		reason: DF.SmallText | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		risk_level: DF.Data | None
		timestamp: DF.Datetime | None
		tool: DF.Data | None
		user: DF.Link | None
	# end: auto-generated types

	def before_insert(self):
		# stamped server-side: the caller does not get to choose when its
		# action happened, or on whose behalf it was recorded
		self.timestamp = now_datetime()
		self.user = frappe.session.user

	def validate(self):
		# belt and braces behind the permission model: no role has write, so a
		# save that carries a previous version means something went around the
		# desk. ``get_doc_before_save()`` is None on insert and populated on
		# every update, which makes this check exact.
		if self.get_doc_before_save():
			frappe.throw(_("Agent Action Log entries are append-only and cannot be modified."))
