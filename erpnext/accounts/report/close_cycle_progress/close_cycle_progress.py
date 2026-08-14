# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from erpnext.accounts.closing.sequencing import DONE_STATUSES

TASK_STATUSES = ("Pending", "In Progress", "Blocked", "Completed", "Skipped")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart(data)
	return columns, data, None, chart


def get_columns():
	return [
		{
			"fieldname": "close_cycle",
			"label": _("Close Cycle"),
			"fieldtype": "Link",
			"options": "Close Cycle",
			"width": 140,
		},
		{
			"fieldname": "task",
			"label": _("Task"),
			"fieldtype": "Link",
			"options": "Close Task",
			"width": 120,
		},
		{"fieldname": "task_title", "label": _("Title"), "fieldtype": "Data", "width": 220},
		{"fieldname": "task_type", "label": _("Type"), "fieldtype": "Data", "width": 160},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 110},
		{"fieldname": "due_date", "label": _("Due Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "days_overdue", "label": _("Days Overdue"), "fieldtype": "Int", "width": 110},
		{
			"fieldname": "owner_role",
			"label": _("Owner Role"),
			"fieldtype": "Link",
			"options": "Role",
			"width": 130,
		},
		{
			"fieldname": "assigned_to",
			"label": _("Assigned To"),
			"fieldtype": "Link",
			"options": "User",
			"width": 140,
		},
		{
			"fieldname": "signed_off_by",
			"label": _("Signed Off By"),
			"fieldtype": "Link",
			"options": "User",
			"width": 140,
		},
		{
			"fieldname": "signed_off_on",
			"label": _("Signed Off On"),
			"fieldtype": "Datetime",
			"width": 150,
		},
	]


def get_data(filters):
	task_filters = {}

	if filters.get("close_cycle"):
		task_filters["close_cycle"] = filters.close_cycle
	elif filters.get("company"):
		cycles = frappe.get_all("Close Cycle", filters={"company": filters.company}, pluck="name")
		if not cycles:
			return []
		task_filters["close_cycle"] = ("in", cycles)

	if filters.get("status"):
		task_filters["status"] = filters.status

	tasks = frappe.get_all(
		"Close Task",
		filters=task_filters,
		fields=[
			"name",
			"close_cycle",
			"task_title",
			"task_type",
			"status",
			"due_date",
			"owner_role",
			"assigned_to",
			"signed_off_by",
			"signed_off_on",
		],
		order_by="close_cycle asc, due_date asc, name asc",
	)

	today = getdate(nowdate())
	data = []
	for task in tasks:
		days_overdue = 0
		if task.due_date and task.status not in DONE_STATUSES:
			days_overdue = max(0, (today - getdate(task.due_date)).days)
		data.append(
			{
				"close_cycle": task.close_cycle,
				"task": task.name,
				"task_title": task.task_title,
				"task_type": task.task_type,
				"status": task.status,
				"due_date": task.due_date,
				"days_overdue": days_overdue,
				"owner_role": task.owner_role,
				"assigned_to": task.assigned_to,
				"signed_off_by": task.signed_off_by,
				"signed_off_on": task.signed_off_on,
			}
		)

	return data


def get_chart(data):
	counts = dict.fromkeys(TASK_STATUSES, 0)
	for row in data:
		if row["status"] in counts:
			counts[row["status"]] += 1

	return {
		"data": {
			"labels": [_(status) for status in TASK_STATUSES],
			"datasets": [{"name": _("Tasks"), "values": [counts[status] for status in TASK_STATUSES]}],
		},
		"type": "bar",
		"colors": ["#ecad4b", "#5e64ff", "#ff5858", "#28a745", "#b8c2cc"],
	}
