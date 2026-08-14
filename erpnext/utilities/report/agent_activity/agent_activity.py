# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Agent Activity: what the agent surface actually did, and what it was refused.

The auditor's view of Blueprint W6's "every agent action logged". Rows come
straight from the Agent Action Log; the summary cards answer the three
questions an auditor asks first — how much did agents do, how often were
they stopped, and how often did a permitted call fail anyway.

A rising **denied** count is not a bug report: it is the policy engine
working. A rising **error rate** is.
"""

import frappe
from frappe import _
from frappe.utils import flt

LOG_DOCTYPE = "Agent Action Log"

GROUP_BY_FIELD = {"Tool": "tool", "Decision": "decision", "Risk Level": "risk_level", "User": "user"}

DECISION_COLORS = {"Allowed": "#28a745", "Denied": "#ff5858"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	data = get_data(filters)
	return get_columns(), data, None, get_chart(data, filters), get_report_summary(data)


def get_columns():
	return [
		{"label": _("Log"), "fieldname": "name", "fieldtype": "Link", "options": LOG_DOCTYPE, "width": 130},
		{"label": _("Timestamp"), "fieldname": "timestamp", "fieldtype": "Datetime", "width": 165},
		{"label": _("User"), "fieldname": "user", "fieldtype": "Link", "options": "User", "width": 170},
		{"label": _("Tool"), "fieldname": "tool", "fieldtype": "Data", "width": 150},
		{"label": _("Risk"), "fieldname": "risk_level", "fieldtype": "Data", "width": 110},
		{"label": _("Decision"), "fieldname": "decision", "fieldtype": "Data", "width": 100},
		{"label": _("Outcome"), "fieldname": "outcome", "fieldtype": "Data", "width": 100},
		{"label": _("Doctype"), "fieldname": "doctype_touched", "fieldtype": "Data", "width": 140},
		{
			"label": _("Document"),
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 150,
		},
		{
			"label": _("Reference Doctype"),
			"fieldname": "reference_doctype",
			"fieldtype": "Link",
			"options": "DocType",
			"width": 130,
			"hidden": 1,
		},
		{"label": _("Duration (ms)"), "fieldname": "duration_ms", "fieldtype": "Int", "width": 110},
		{"label": _("Reason / Error"), "fieldname": "detail", "fieldtype": "Data", "width": 420},
	]


def get_data(filters):
	conditions = {}
	if filters.get("user"):
		conditions["user"] = filters.user
	if filters.get("tool"):
		conditions["tool"] = filters.tool
	if filters.get("decision"):
		conditions["decision"] = filters.decision
	if filters.get("outcome"):
		conditions["outcome"] = filters.outcome
	if filters.get("risk_level"):
		conditions["risk_level"] = filters.risk_level

	if filters.get("from_date") and filters.get("to_date"):
		conditions["timestamp"] = ("between", [filters.from_date, filters.to_date])
	elif filters.get("from_date"):
		conditions["timestamp"] = (">=", filters.from_date)
	elif filters.get("to_date"):
		conditions["timestamp"] = ("<=", filters.to_date)

	rows = frappe.get_all(
		LOG_DOCTYPE,
		filters=conditions,
		fields=[
			"name",
			"timestamp",
			"user",
			"tool",
			"risk_level",
			"decision",
			"outcome",
			"doctype_touched",
			"reference_doctype",
			"reference_name",
			"duration_ms",
			"reason",
			"error",
		],
		order_by="timestamp desc",
		limit_page_length=0,
	)

	for row in rows:
		# one column, because a reader wants "what happened" in one place:
		# the error when there is one, the policy reason otherwise
		row.detail = row.error or row.reason

	return rows


def get_chart(data, filters):
	group_by = filters.get("group_by") or "Tool"
	fieldname = GROUP_BY_FIELD.get(group_by, "tool")

	counts = {}
	for row in data:
		key = row.get(fieldname) or _("(unset)")
		counts[key] = counts.get(key, 0) + 1

	# busiest first, and never more bars than a chart can carry
	ordered = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[:12]

	chart = {
		"data": {
			"labels": [str(label) for label, _count in ordered],
			"datasets": [{"name": _("Calls"), "values": [count for _label, count in ordered]}],
		},
		"type": "bar",
	}

	if fieldname == "decision":
		chart["colors"] = [DECISION_COLORS.get(str(label), "#adb5bd") for label, _count in ordered]

	return chart


def get_report_summary(data):
	total = len(data)
	denied = sum(1 for row in data if row.decision == "Denied")
	errors = sum(1 for row in data if row.outcome == "Error")
	writes = sum(
		1 for row in data if row.risk_level and row.risk_level != "READ" and row.outcome == "Success"
	)
	error_rate = flt(errors * 100.0 / total, 2) if total else 0.0

	return [
		{"label": _("Total Calls"), "value": total, "indicator": "Blue", "datatype": "Int"},
		{
			"label": _("Denied by Policy"),
			"value": denied,
			"indicator": "Orange" if denied else "Green",
			"datatype": "Int",
		},
		{
			"label": _("Error Rate"),
			"value": error_rate,
			"indicator": "Red" if error_rate > 5 else "Green",
			"datatype": "Percent",
		},
		{
			"label": _("Successful Writes"),
			"value": writes,
			"indicator": "Blue",
			"datatype": "Int",
		},
	]
