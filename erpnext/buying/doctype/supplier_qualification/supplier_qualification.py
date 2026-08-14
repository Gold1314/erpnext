# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate

from erpnext.buying.sourcing.engine import (
	count_expiring,
	documents_ok,
	evaluate_documents,
	score_questionnaire,
)
from erpnext.buying.sourcing.models import DocumentRequirement, QuestionnaireAnswer

STATUS_DRAFT = "Draft"
STATUS_QUALIFIED = "Qualified"
STATUS_REJECTED = "Rejected"
STATUS_EXPIRED = "Expired"


class SupplierQualification(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.buying.doctype.supplier_document_requirement.supplier_document_requirement import (
			SupplierDocumentRequirement,
		)
		from erpnext.buying.doctype.supplier_qualification_answer.supplier_qualification_answer import (
			SupplierQualificationAnswer,
		)

		amended_from: DF.Link | None
		answers: DF.Table[SupplierQualificationAnswer]
		company: DF.Link | None
		documents: DF.Table[SupplierDocumentRequirement]
		knockout_failures: DF.SmallText | None
		max_score: DF.Float
		notes: DF.SmallText | None
		qualification_date: DF.Date | None
		risk_tier: DF.Data | None
		score_percent: DF.Percent
		status: DF.Literal["Draft", "Qualified", "Rejected", "Expired"]
		supplier: DF.Link
		supplier_name: DF.Data | None
		template: DF.Link
		total_score: DF.Float
		valid_until: DF.Date | None
	# end: auto-generated types

	def validate(self):
		self.validate_dates()
		self.validate_answers()
		self.evaluate()

	def validate_dates(self):
		if self.valid_until and self.qualification_date:
			if getdate(self.valid_until) < getdate(self.qualification_date):
				frappe.throw(_("Valid Until cannot be before the Qualification Date."))

	def validate_answers(self):
		for row in self.answers:
			if flt(row.score) < 0:
				frappe.throw(_("Row {0}: Score cannot be negative.").format(row.idx))
			if flt(row.max_score) > 0 and flt(row.score) > flt(row.max_score):
				frappe.throw(
					_("Row {0}: Score {1} is above the max score {2}.").format(
						row.idx, flt(row.score), flt(row.max_score)
					)
				)

	# -- scoring ---------------------------------------------------------
	def evaluate(self):
		"""Run the pure engine over the answers and documents.

		``as_of`` is passed in from ``frappe.utils.nowdate()`` so the engine
		itself stays free of ambient time (see ``erpnext/buying/sourcing``).
		"""
		template = frappe.get_cached_doc("Supplier Qualification Template", self.template)

		result = score_questionnaire(
			[
				QuestionnaireAnswer(
					question_key=row.question,
					weight=flt(row.weight),
					score=flt(row.score),
					max_score=flt(row.max_score),
					is_knockout=bool(row.is_knockout),
					passed=bool(row.passed),
				)
				for row in self.answers
			],
			pass_threshold_pct=flt(template.pass_threshold),
			risk_bands=template.get_risk_bands(),
		)

		self.total_score = result.total_score
		self.max_score = result.max_score
		self.score_percent = result.percent
		self.risk_tier = result.risk_tier
		self.knockout_failures = "\n".join(result.knockout_failures) if result.knockout_failures else None

		statuses = self.evaluate_document_rows()
		for row, status in zip(self.documents, statuses, strict=False):
			row.doc_status = status.status

		self.flags.qualification_passed = result.passed and documents_ok(statuses, mandatory_only=True)

		if self.docstatus == 0:
			# preview only - the real transition happens on submit
			self.status = STATUS_DRAFT

	def evaluate_document_rows(self):
		return evaluate_documents(
			[
				DocumentRequirement(
					doc_type=row.document_type,
					is_mandatory=bool(row.is_mandatory),
					requires_expiry=bool(row.requires_expiry),
					provided=bool(row.attachment),
					expiry_date=row.expiry_date,
				)
				for row in self.documents
			],
			as_of=getdate(nowdate()),
		)

	# -- lifecycle -------------------------------------------------------
	def on_submit(self):
		if not self.answers:
			frappe.throw(_("Load the questionnaire before submitting."))

		passed = self.flags.get("qualification_passed")
		if passed is None:
			self.evaluate()
			passed = self.flags.get("qualification_passed")

		self.db_set("status", STATUS_QUALIFIED if passed else STATUS_REJECTED)

	def on_cancel(self):
		self.db_set("status", STATUS_DRAFT)

	# -- actions ---------------------------------------------------------
	@frappe.whitelist()
	def load_template(self):
		"""Copy the template's questions and required documents onto this doc.

		Existing rows are replaced, so the action doubles as "reset to the
		template". Scores captured so far are re-applied by question text
		where the question still exists in the template.
		"""
		if self.docstatus != 0:
			frappe.throw(_("Only a draft qualification can load a template."))

		template = frappe.get_doc("Supplier Qualification Template", self.template)
		if not template.enabled:
			frappe.throw(_("Qualification Template {0} is disabled.").format(template.name))

		previous_scores = {
			(row.question or "").strip(): (flt(row.score), row.passed, row.remarks) for row in self.answers
		}
		previous_docs = {
			(row.document_type or "").strip(): (row.attachment, row.expiry_date) for row in self.documents
		}

		self.set("answers", [])
		for row in template.questions:
			score, passed, remarks = previous_scores.get((row.question or "").strip(), (0.0, 1, None))
			self.append(
				"answers",
				{
					"question": row.question,
					"category": row.category,
					"weight": row.weight,
					"max_score": row.max_score,
					"is_knockout": row.is_knockout,
					"score": score,
					"passed": passed,
					"remarks": remarks,
				},
			)

		self.set("documents", [])
		for row in template.required_documents:
			attachment, expiry_date = previous_docs.get((row.document_type or "").strip(), (None, None))
			self.append(
				"documents",
				{
					"document_type": row.document_type,
					"is_mandatory": row.is_mandatory,
					"requires_expiry": row.requires_expiry,
					"attachment": attachment,
					"expiry_date": expiry_date,
				},
			)

		return {"questions": len(self.answers), "documents": len(self.documents)}

	@frappe.whitelist()
	def get_document_summary(self):
		"""Document statuses for the form's dashboard/indicator area."""
		statuses = self.evaluate_document_rows()
		return {
			"statuses": [
				{
					"document_type": status.doc_type,
					"status": status.status,
					"days_to_expiry": status.days_to_expiry,
					"is_mandatory": status.is_mandatory,
				}
				for status in statuses
			],
			"mandatory_ok": documents_ok(statuses, mandatory_only=True),
			"expiring": count_expiring(statuses),
		}


@frappe.whitelist()
def get_supplier_qualification(supplier: str, as_of: str | None = None) -> dict | None:
	"""Latest submitted, unexpired qualification for a supplier.

	Returns ``None`` when the supplier has never been qualified or the most
	recent qualification has lapsed. Consumed by sourcing award scoring
	(``Sourcing Event.compute_award_analysis``) and by the recommended
	PO-gating hook described in ``erpnext/buying/sourcing/DESIGN.md``.
	"""
	as_of = getdate(as_of or nowdate())

	rows = frappe.get_all(
		"Supplier Qualification",
		filters={"supplier": supplier, "docstatus": 1},
		fields=[
			"name",
			"template",
			"status",
			"score_percent",
			"risk_tier",
			"qualification_date",
			"valid_until",
		],
		order_by="qualification_date desc, creation desc",
		limit=1,
	)
	if not rows:
		return None

	qualification = rows[0]
	if qualification.valid_until and getdate(qualification.valid_until) < as_of:
		return None
	if qualification.status == STATUS_EXPIRED:
		return None

	qualification["is_qualified"] = qualification.status == STATUS_QUALIFIED
	return qualification


def expire_qualifications(as_of: str | None = None) -> int:
	"""Mark submitted qualifications past their ``valid_until`` as Expired.

	Scheduler-friendly (idempotent, no document hooks fired). The daily hook
	is *recommended* in ``erpnext/buying/sourcing/DESIGN.md`` - it is not
	registered in ``hooks.py`` by this change.
	"""
	as_of = getdate(as_of or nowdate())

	names = frappe.get_all(
		"Supplier Qualification",
		filters={
			"docstatus": 1,
			"status": ("in", (STATUS_QUALIFIED, STATUS_REJECTED)),
			"valid_until": ("<", as_of),
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Supplier Qualification", name, "status", STATUS_EXPIRED)

	return len(names)
