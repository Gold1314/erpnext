# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Staging document for an LLM-extracted supplier invoice.

The unstructured twin of ``Inbound E-Invoice``: everything the model read
from a pasted / e-mailed / uploaded document, held for human review. No
ledger entry exists until an operator explicitly creates a **draft**
Purchase Invoice from it.

Orchestration lives in :mod:`erpnext.ai.api`; this class only holds the
invariants that must survive a manual edit of the form.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class AIDocumentExtraction(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.utilities.doctype.ai_document_extraction_item.ai_document_extraction_item import (
			AIDocumentExtractionItem,
		)
		from erpnext.utilities.doctype.ai_document_extraction_tax.ai_document_extraction_tax import (
			AIDocumentExtractionTax,
		)

		company: DF.Link
		confidence_threshold: DF.Percent
		currency: DF.Link | None
		due_date: DF.Date | None
		extraction_json: DF.LongText | None
		grand_total: DF.Currency
		invoice_date: DF.Date | None
		invoice_number: DF.Data | None
		items: DF.Table[AIDocumentExtractionItem]
		model_used: DF.Data | None
		needs_review_fields: DF.SmallText | None
		net_total: DF.Currency
		provider_used: DF.Data | None
		purchase_invoice: DF.Link | None
		raw_text: DF.LongText | None
		source_file: DF.Attach | None
		source_type: DF.Literal["Pasted Text", "File", "Email"]
		status: DF.Literal[
			"Pending Extraction", "Extracted", "Needs Review", "Invoice Created", "Failed", "Rejected"
		]
		supplier: DF.Link | None
		supplier_name_extracted: DF.Data | None
		supplier_vat: DF.Data | None
		tax_total: DF.Currency
		taxes: DF.Table[AIDocumentExtractionTax]
		warnings: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		self.validate_supplier_company()
		self.sync_status_with_invoice()

	def validate_supplier_company(self):
		"""A resolved supplier and accounts must be usable for this company's books.

		Mirror of ``InboundEInvoice.validate_supplier_company`` — same staging
		doctrine, same invariant.
		"""
		if self.supplier and frappe.get_cached_value("Supplier", self.supplier, "disabled"):
			frappe.throw(_("Supplier {0} is disabled").format(self.supplier))

		for row in self.items:
			if row.expense_account:
				account_company = frappe.get_cached_value("Account", row.expense_account, "company")
				if account_company != self.company:
					frappe.throw(
						_("Row {0}: expense account {1} belongs to {2}, not {3}").format(
							row.idx, row.expense_account, account_company, self.company
						)
					)

		for row in self.taxes:
			if row.account_head:
				account_company = frappe.get_cached_value("Account", row.account_head, "company")
				if account_company != self.company:
					frappe.throw(
						_("Tax row {0}: account {1} belongs to {2}, not {3}").format(
							row.idx, row.account_head, account_company, self.company
						)
					)

	def sync_status_with_invoice(self):
		"""The linked Purchase Invoice is the source of truth for the status."""
		if self.status == "Invoice Created" and not self.purchase_invoice:
			self.status = "Needs Review"
		if self.purchase_invoice and not frappe.db.exists("Purchase Invoice", self.purchase_invoice):
			self.purchase_invoice = None
			self.status = "Needs Review"
