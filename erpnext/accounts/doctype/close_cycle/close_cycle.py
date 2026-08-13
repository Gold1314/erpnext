# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from erpnext.accounts.closing.sequencing import (
	DONE_STATUSES,
	compute_due_date,
	resolve_dependencies,
	topological_order,
)


class CloseCycle(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		close_task_template: DF.Link | None
		company: DF.Link
		notes: DF.SmallText | None
		period_end_date: DF.Date
		period_start_date: DF.Date | None
		progress: DF.Percent
		status: DF.Literal["Open", "In Progress", "Completed"]
	# end: auto-generated types

	def validate(self):
		if self.period_start_date and getdate(self.period_start_date) > getdate(self.period_end_date):
			frappe.throw(_("Period Start Date cannot be after Period End Date."))
		self.validate_template_company()

	def validate_template_company(self):
		if not self.close_task_template:
			return
		template_company = frappe.db.get_value("Close Task Template", self.close_task_template, "company")
		if template_company and template_company != self.company:
			frappe.throw(
				_("Close Task Template {0} belongs to company {1}.").format(
					self.close_task_template, template_company
				)
			)

	def after_insert(self):
		if self.close_task_template:
			self.create_tasks()

	@frappe.whitelist()
	def create_tasks(self):
		"""Materialize one Close Task per row of the linked template.

		Dependencies declared as comma-separated titles on the template are
		resolved to the created Close Task names; due dates are computed as
		period end date + offset in *calendar* days (see
		``erpnext.accounts.closing.sequencing.compute_due_date``).
		"""
		if not self.close_task_template:
			frappe.throw(_("Select a Close Task Template first."))
		if frappe.db.exists("Close Task", {"close_cycle": self.name}):
			frappe.throw(_("Tasks have already been created for this Close Cycle."))

		template = frappe.get_doc("Close Task Template", self.close_task_template)
		if not template.tasks:
			frappe.throw(_("Close Task Template {0} has no tasks.").format(template.name))

		rows = [
			{"title": row.task_title, "depends_on_titles": row.depends_on_titles}
			for row in template.tasks
		]
		try:
			dependency_map = resolve_dependencies(rows)
			creation_order = topological_order(dependency_map)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid Task Dependencies"))

		row_by_title = {row.task_title: row for row in template.tasks}
		name_by_title = {}
		period_end = getdate(self.period_end_date)

		for title in creation_order:
			row = row_by_title[title]
			task = frappe.new_doc("Close Task")
			task.update(
				{
					"close_cycle": self.name,
					"task_title": row.task_title,
					"task_type": row.task_type,
					"owner_role": row.owner_role,
					"due_date": compute_due_date(period_end, row.due_day_offset),
					"instructions": row.instructions,
					"auto_verify": row.auto_verify,
				}
			)
			for dep_title in dependency_map[title]:
				task.append("dependencies", {"close_task": name_by_title[dep_title]})
			task.insert(ignore_permissions=True)
			name_by_title[title] = task.name

		update_cycle_progress(self.name)
		return list(name_by_title.values())


def update_cycle_progress(close_cycle):
	"""Recompute a cycle's progress % and status from its tasks.

	Called by the Close Task controller on every task update/delete. A task
	counts as done when Completed or Skipped. Status: Open (no tasks or all
	Pending), Completed (all done), otherwise In Progress. Writes via
	``frappe.db.set_value`` to avoid re-triggering document hooks.
	"""
	if not frappe.db.exists("Close Cycle", close_cycle):
		return

	statuses = frappe.get_all("Close Task", filters={"close_cycle": close_cycle}, pluck="status")
	total = len(statuses)
	done = sum(1 for status in statuses if status in DONE_STATUSES)

	if not total:
		status, progress = "Open", 0
	elif done == total:
		status, progress = "Completed", 100
	elif all(s == "Pending" for s in statuses):
		status, progress = "Open", 0
	else:
		status, progress = "In Progress", flt(done * 100.0 / total, 2)

	frappe.db.set_value(
		"Close Cycle", close_cycle, {"status": status, "progress": progress}, update_modified=False
	)
