# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext.buying.sourcing.models import RISK_HIGH, RISK_LOW, RISK_MEDIUM

DEFAULT_TEMPLATE_NAME = "Standard Supplier Qualification"


class SupplierQualificationTemplate(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.buying.doctype.supplier_document_requirement.supplier_document_requirement import (
			SupplierDocumentRequirement,
		)
		from erpnext.buying.doctype.supplier_qualification_template_item.supplier_qualification_template_item import (
			SupplierQualificationTemplateItem,
		)

		enabled: DF.Check
		low_risk_threshold: DF.Percent
		medium_risk_threshold: DF.Percent
		pass_threshold: DF.Percent
		questions: DF.Table[SupplierQualificationTemplateItem]
		required_documents: DF.Table[SupplierDocumentRequirement]
		supplier_group: DF.Link | None
		template_name: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_questions()
		self.validate_thresholds()
		self.validate_documents()

	def validate_questions(self):
		if not self.questions:
			frappe.throw(_("Add at least one question to the questionnaire."))

		for row in self.questions:
			if flt(row.weight) < 0:
				frappe.throw(_("Row {0}: Weight cannot be negative.").format(row.idx))
			if flt(row.max_score) < 0:
				frappe.throw(_("Row {0}: Max Score cannot be negative.").format(row.idx))

		if not any(flt(row.weight) > 0 and flt(row.max_score) > 0 for row in self.questions):
			frappe.throw(_("At least one question must carry a weight and a max score above zero."))

	def validate_thresholds(self):
		for fieldname in ("pass_threshold", "low_risk_threshold", "medium_risk_threshold"):
			value = flt(self.get(fieldname))
			if value < 0 or value > 100:
				frappe.throw(_("{0} must be between 0 and 100.").format(_(self.meta.get_label(fieldname))))

		if flt(self.low_risk_threshold) < flt(self.medium_risk_threshold):
			frappe.throw(_("Low Risk Threshold cannot be lower than Medium Risk Threshold."))

	def validate_documents(self):
		seen = set()
		for row in self.required_documents:
			key = (row.document_type or "").strip().lower()
			if key in seen:
				frappe.throw(_("Document Type {0} is listed more than once.").format(row.document_type))
			seen.add(key)

	def get_risk_bands(self) -> list[tuple[float, str]]:
		"""Risk bands for ``erpnext.buying.sourcing.engine.score_questionnaire``."""
		return [
			(flt(self.low_risk_threshold), RISK_LOW),
			(flt(self.medium_risk_threshold), RISK_MEDIUM),
			(0.0, RISK_HIGH),
		]


DEFAULT_QUESTIONS = [
	{
		"question": "Audited financial statements for the last two years show a positive net worth",
		"category": "Financial",
		"weight": 2,
		"max_score": 5,
		"guidance": "Score 5 when both years are positive and improving, 0 when net worth is negative.",
	},
	{
		"question": "Credit rating / days-beyond-terms history is acceptable",
		"category": "Financial",
		"weight": 1,
		"max_score": 5,
	},
	{
		"question": "Certified quality management system in place (ISO 9001 or equivalent)",
		"category": "Quality",
		"weight": 2,
		"max_score": 5,
	},
	{
		"question": "Documented incoming inspection and non-conformance process",
		"category": "Quality",
		"weight": 1,
		"max_score": 5,
	},
	{
		"question": "No unresolved sanctions, debarment or adverse media findings",
		"category": "Compliance",
		"weight": 2,
		"max_score": 5,
		"is_knockout": 1,
		"guidance": "Knockout: an unresolved sanctions hit rejects the supplier regardless of score.",
	},
	{
		"question": "Signed code of conduct and anti-bribery policy",
		"category": "Compliance",
		"weight": 1,
		"max_score": 5,
		"is_knockout": 1,
	},
	{
		"question": "Demonstrated capacity to meet our forecast volumes",
		"category": "Capability",
		"weight": 2,
		"max_score": 5,
	},
	{
		"question": "Business continuity / disaster recovery plan tested in the last 12 months",
		"category": "Capability",
		"weight": 1,
		"max_score": 5,
	},
	{
		"question": "Environmental and labour practices meet our supplier code",
		"category": "Sustainability",
		"weight": 1,
		"max_score": 5,
	},
]

DEFAULT_DOCUMENTS = [
	{"document_type": "ISO 9001 Certificate", "is_mandatory": 1, "requires_expiry": 1},
	{"document_type": "Liability Insurance Certificate", "is_mandatory": 1, "requires_expiry": 1},
	{"document_type": "Tax Clearance Certificate", "is_mandatory": 1, "requires_expiry": 1},
	{"document_type": "Signed Code of Conduct", "is_mandatory": 1, "requires_expiry": 0},
	{"document_type": "Bank Details Letter", "is_mandatory": 0, "requires_expiry": 0},
]


@frappe.whitelist()
def create_default_qualification_template():
	"""Install the shipped ``Standard Supplier Qualification`` template.

	Idempotent: an existing template of that name is left untouched and
	reported back as skipped (same contract as
	``close_task_template.create_default_close_template``).
	"""
	frappe.only_for(("Purchase Manager", "System Manager"))

	if frappe.db.exists("Supplier Qualification Template", DEFAULT_TEMPLATE_NAME):
		return {"created": [], "skipped": [DEFAULT_TEMPLATE_NAME]}

	template = frappe.new_doc("Supplier Qualification Template")
	template.template_name = DEFAULT_TEMPLATE_NAME
	template.enabled = 1
	template.pass_threshold = 70
	template.low_risk_threshold = 85
	template.medium_risk_threshold = 70

	for row in DEFAULT_QUESTIONS:
		template.append("questions", row)
	for row in DEFAULT_DOCUMENTS:
		template.append("required_documents", row)

	template.insert(ignore_permissions=True)
	return {"created": [template.name], "skipped": []}
