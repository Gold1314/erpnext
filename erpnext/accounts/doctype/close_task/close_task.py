# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from erpnext.accounts.closing.sequencing import blocking_dependencies


class CloseTask(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.close_task_dependency.close_task_dependency import (
			CloseTaskDependency,
		)

		assigned_to: DF.Link | None
		auto_verify: DF.Check
		close_cycle: DF.Link
		dependencies: DF.Table[CloseTaskDependency]
		due_date: DF.Date | None
		evidence: DF.Attach | None
		instructions: DF.SmallText | None
		owner_role: DF.Link | None
		signed_off_by: DF.Link | None
		signed_off_on: DF.Datetime | None
		status: DF.Literal["Pending", "In Progress", "Blocked", "Completed", "Skipped"]
		task_title: DF.Data
		task_type: DF.Literal[
			"Manual",
			"Period Closing Voucher",
			"Exchange Rate Revaluation",
			"Deferred Accounting",
			"Bank Reconciliation",
			"Ledger Health",
			"Accounting Period Lock",
		]
		verification_result: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		self.validate_dependency_rows()
		self.validate_completion()

	def validate_dependency_rows(self):
		"""Dependencies must be other tasks of the same Close Cycle."""
		for row in self.dependencies:
			if not row.close_task:
				continue
			if row.close_task == self.name:
				frappe.throw(_("A task cannot depend on itself."))
			dep_cycle = frappe.db.get_value("Close Task", row.close_task, "close_cycle")
			if dep_cycle != self.close_cycle:
				frappe.throw(
					_("Dependency {0} belongs to Close Cycle {1}, not {2}.").format(
						row.close_task, dep_cycle, self.close_cycle
					)
				)

	def validate_completion(self):
		"""Completing a task requires all dependencies done; stamp the sign-off."""
		if self.status == "Completed":
			blocking = self.get_blocking_dependencies()
			if blocking:
				titles = frappe.get_all(
					"Close Task", filters={"name": ("in", blocking)}, pluck="task_title"
				)
				frappe.throw(
					_("Cannot complete this task. Open dependencies: {0}").format(
						", ".join(titles or blocking)
					),
					title=_("Dependencies Not Completed"),
				)
			if not self.signed_off_by:
				self.signed_off_by = frappe.session.user
				self.signed_off_on = now_datetime()
		elif self.signed_off_by or self.signed_off_on:
			# reverted from Completed - the previous sign-off no longer applies
			self.signed_off_by = None
			self.signed_off_on = None

	def get_blocking_dependencies(self):
		"""Names of dependency tasks that are not yet Completed/Skipped."""
		dep_names = [row.close_task for row in self.dependencies if row.close_task]
		if not dep_names:
			return []
		status_map = dict(
			frappe.get_all(
				"Close Task",
				filters={"name": ("in", dep_names)},
				fields=["name", "status"],
				as_list=True,
			)
		)
		return blocking_dependencies(status_map, dep_names)

	@frappe.whitelist()
	def verify(self):
		"""Run this task's auto-verification and store the result.

		Returns ``{"ok": bool, "message": str, "status": str}``. See
		``erpnext.accounts.closing.verifications.verify_task`` for the rules.
		"""
		from erpnext.accounts.closing.verifications import verify_task

		ok, message = verify_task(self)
		return {"ok": ok, "message": message, "status": self.status}

	def on_update(self):
		self.update_cycle()

	def after_delete(self):
		self.update_cycle()

	def update_cycle(self):
		from erpnext.accounts.doctype.close_cycle.close_cycle import update_cycle_progress

		update_cycle_progress(self.close_cycle)
