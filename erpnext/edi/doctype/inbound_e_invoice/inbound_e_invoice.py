# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Staging document for a received supplier e-invoice.

Everything a supplier sent us, parsed but *not yet posted*: the resolution
gaps (unknown supplier, unmapped item) are visible and editable here, and no
ledger entry exists until a human clicks through to a draft Purchase Invoice.

Orchestration lives in :mod:`erpnext.edi.inbound.api`; this class only holds
the invariants that must survive a manual edit of the form.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class InboundEInvoice(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.edi.doctype.inbound_e_invoice_item.inbound_e_invoice_item import InboundEInvoiceItem
		from erpnext.edi.doctype.inbound_e_invoice_tax.inbound_e_invoice_tax import InboundEInvoiceTax

		company: DF.Link
		currency: DF.Link | None
		due_date: DF.Date | None
		exceptions: DF.SmallText | None
		grand_total: DF.Currency
		invoice_id: DF.Data
		issue_date: DF.Date | None
		items: DF.Table[InboundEInvoiceItem]
		net_total: DF.Currency
		order_reference: DF.Data | None
		purchase_invoice: DF.Link | None
		raw_profile: DF.Data | None
		source_file: DF.Attach | None
		status: DF.Literal["Pending Review", "Matched", "Exception", "Invoice Created", "Rejected"]
		supplier: DF.Link | None
		supplier_name_parsed: DF.Data | None
		supplier_vat: DF.Data | None
		tax_total: DF.Currency
		taxes: DF.Table[InboundEInvoiceTax]
		tolerance_pct: DF.Percent
		warnings: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		self.validate_supplier_company()
		self.sync_status_with_invoice()

	def validate_supplier_company(self):
		"""A resolved supplier must be usable for this company's books."""
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

	def sync_status_with_invoice(self):
		"""The linked Purchase Invoice is the source of truth for the status.

		Guards against a stale *Invoice Created* left behind when the draft
		it produced was deleted — the operator can then re-create it instead
		of being stuck on a document that points nowhere.
		"""
		if self.status == "Invoice Created" and not self.purchase_invoice:
			self.status = "Pending Review"
		if self.purchase_invoice and not frappe.db.exists("Purchase Invoice", self.purchase_invoice):
			self.purchase_invoice = None
			self.status = "Pending Review"
