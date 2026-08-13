# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from erpnext.buying.sourcing.engine import count_expiring, documents_ok, evaluate_documents
from erpnext.buying.sourcing.models import RISK_HIGH, RISK_LOW, RISK_MEDIUM, DocumentRequirement

RISK_TIERS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	data = get_data(filters)
	return get_columns(), data, None, get_chart(data)


def get_columns():
	return [
		{
			"fieldname": "supplier",
			"label": _("Supplier"),
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 160,
		},
		{"fieldname": "supplier_name", "label": _("Supplier Name"), "fieldtype": "Data", "width": 180},
		{
			"fieldname": "supplier_group",
			"label": _("Supplier Group"),
			"fieldtype": "Link",
			"options": "Supplier Group",
			"width": 130,
		},
		{
			"fieldname": "qualification",
			"label": _("Qualification"),
			"fieldtype": "Link",
			"options": "Supplier Qualification",
			"width": 130,
		},
		{
			"fieldname": "template",
			"label": _("Template"),
			"fieldtype": "Link",
			"options": "Supplier Qualification Template",
			"width": 180,
		},
		{"fieldname": "score_percent", "label": _("Score %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "risk_tier", "label": _("Risk Tier"), "fieldtype": "Data", "width": 90},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
		{"fieldname": "valid_until", "label": _("Valid Until"), "fieldtype": "Date", "width": 100},
		{
			"fieldname": "days_to_expiry",
			"label": _("Days to Expiry"),
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"fieldname": "mandatory_docs_ok",
			"label": _("Mandatory Docs OK"),
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"fieldname": "expiring_docs",
			"label": _("Expiring Docs"),
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"fieldname": "knockout_failures",
			"label": _("Knockout Failures"),
			"fieldtype": "Data",
			"width": 220,
		},
	]


def get_data(filters):
	as_of = getdate(nowdate())
	window = cint(filters.get("expiring_within_days")) or 30

	qualification_filters = {"docstatus": 1}
	if filters.get("company"):
		qualification_filters["company"] = filters.company
	if filters.get("supplier"):
		qualification_filters["supplier"] = filters.supplier

	rows = frappe.get_all(
		"Supplier Qualification",
		filters=qualification_filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"template",
			"status",
			"score_percent",
			"risk_tier",
			"qualification_date",
			"valid_until",
			"knockout_failures",
		],
		order_by="supplier asc, qualification_date desc, creation desc",
	)

	# latest qualification per supplier (the list is already newest-first per supplier)
	latest = {}
	for row in rows:
		latest.setdefault(row.supplier, row)

	supplier_groups = get_supplier_groups(list(latest))
	data = []

	for supplier, row in sorted(latest.items()):
		supplier_group = supplier_groups.get(supplier)
		if filters.get("supplier_group") and supplier_group != filters.supplier_group:
			continue

		status = row.status
		days_to_expiry = None
		if row.valid_until:
			days_to_expiry = (getdate(row.valid_until) - as_of).days
			if days_to_expiry < 0 and status != "Rejected":
				# the daily expire_qualifications job may not have run yet
				status = "Expired"

		statuses = evaluate_document_rows(row.name, as_of, window)

		if filters.get("status") and status != filters.status:
			continue
		if filters.get("risk_tier") and row.risk_tier != filters.risk_tier:
			continue

		data.append(
			{
				"supplier": supplier,
				"supplier_name": row.supplier_name,
				"supplier_group": supplier_group,
				"qualification": row.name,
				"template": row.template,
				"score_percent": flt(row.score_percent),
				"risk_tier": row.risk_tier,
				"status": status,
				"valid_until": row.valid_until,
				"days_to_expiry": days_to_expiry,
				"mandatory_docs_ok": _("Yes") if documents_ok(statuses, mandatory_only=True) else _("No"),
				"expiring_docs": count_expiring(statuses),
				"knockout_failures": (row.knockout_failures or "").replace("\n", ", "),
			}
		)

	return data


def get_supplier_groups(suppliers):
	if not suppliers:
		return {}
	return {
		row.name: row.supplier_group
		for row in frappe.get_all(
			"Supplier",
			filters={"name": ("in", suppliers)},
			fields=["name", "supplier_group"],
		)
	}


def evaluate_document_rows(qualification, as_of, window):
	docs = frappe.get_all(
		"Supplier Document Requirement",
		filters={"parent": qualification, "parenttype": "Supplier Qualification"},
		fields=["document_type", "is_mandatory", "requires_expiry", "attachment", "expiry_date"],
		order_by="idx asc",
	)
	return evaluate_documents(
		[
			DocumentRequirement(
				doc_type=doc.document_type,
				is_mandatory=bool(doc.is_mandatory),
				requires_expiry=bool(doc.requires_expiry),
				provided=bool(doc.attachment),
				expiry_date=doc.expiry_date,
			)
			for doc in docs
		],
		as_of=as_of,
		expiring_within_days=window,
	)


def get_chart(data):
	counts = dict.fromkeys(RISK_TIERS, 0)
	for row in data:
		if row["risk_tier"] in counts:
			counts[row["risk_tier"]] += 1

	return {
		"data": {
			"labels": [_(tier) for tier in RISK_TIERS],
			"datasets": [{"name": _("Suppliers"), "values": [counts[tier] for tier in RISK_TIERS]}],
		},
		"type": "bar",
		"colors": ["#28a745", "#ecad4b", "#ff5858"],
	}
