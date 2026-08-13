# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for AI document intake.

Lifecycle, tracked on the **AI Document Extraction** staging document::

    (create with text)          extract_document           resolve_masters
    Pending Extraction ──────► Extracted / Needs Review ──────► (enriched)
             │                        │                            │
             └── provider error ──► Failed (re-extract allowed)    │
                                      create_purchase_invoice ◄────┘
                                              │
                                              ▼
                                       Invoice Created

The safety doctrine is inherited **verbatim** from ``erpnext.edi.inbound``
(the structured counterpart of this module):

**Never submit.** :func:`create_purchase_invoice` inserts a *draft* Purchase
Invoice and stops. An LLM's reading of a third-party document must be
approved by a human before it posts to the general ledger.

**Never mint masters.** Suppliers, items, accounts and UOMs are *resolved*,
never created. A failed lookup leaves the field blank with a warning; the
staging document is editable so a human completes it.

**Never double-book.** :func:`create_purchase_invoice` refuses when a draft
or submitted Purchase Invoice already carries the same ``supplier`` +
``bill_no``.

**Stage, don't discard.** A bad extraction lands in *Needs Review* or
*Failed* with its raw model output preserved on the document; nothing is
dropped on the floor.

Where the inbound EDI module's resolution logic factors cleanly
(:func:`resolve_expense_account`, :func:`resolve_tax_account` — both take
plain scalars), it is **imported and reused**. Supplier resolution and the
duplicate-invoice guard are *mirrored* instead: the EDI versions are bound
to its ``ParsedInvoice`` dataclass / ``invoice_id`` fieldname and taking a
dependency on those internals for two small functions would couple the
modules harder than duplicating fifteen documented lines.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, today

from erpnext.ai.prompts import SYSTEM_PROMPT_INVOICE, build_user_content
from erpnext.ai.providers import ProviderError, ProviderNotConfigured, get_provider
from erpnext.ai.schemas import INVOICE_EXTRACTION_SCHEMA, validate_extraction
from erpnext.edi.inbound.mapper import resolve_expense_account, resolve_tax_account

STAGING_DOCTYPE = "AI Document Extraction"

#: statuses from which a Purchase Invoice may still be created
CREATABLE_STATUSES = ("Extracted", "Needs Review")

#: statuses from which (re-)extraction is allowed
EXTRACTABLE_STATUSES = ("Pending Extraction", "Extracted", "Needs Review", "Failed")

#: text file extensions read directly from an attached File; PDFs are
#: refused honestly — no PDF text-extraction library ships with v1
TEXT_FILE_EXTENSIONS = (".txt", ".csv", ".md", ".text")

#: same rounding tolerance as erpnext.edi.inbound.api.AMOUNT_TOLERANCE
AMOUNT_TOLERANCE = 0.02


# -------------------------------------------------------------- extraction


def _get_document_text(doc) -> str:
	"""The text to feed the model, per ``source_type``.

	*Pasted Text* / *Email* read ``raw_text``. *File* reads the attached
	File's content when it is a plain-text format; the text is copied into
	``raw_text`` so the document carries what the model actually saw. PDFs
	are refused with an honest message: no PDF text extractor (pdfplumber /
	pypdf) is available in this environment, and pretending otherwise would
	stage empty extractions.
	"""
	if doc.source_type == "File":
		if doc.raw_text:
			return doc.raw_text
		if not doc.source_file:
			frappe.throw(_("Attach a source file or paste the document text into Raw Text."))

		file_name = (doc.source_file or "").lower()
		if file_name.endswith(".pdf"):
			frappe.throw(
				_(
					"PDF text extraction is not available in this version — copy the invoice text "
					"out of the PDF and paste it into Raw Text."
				),
				title=_("PDF Not Supported Yet"),
			)
		if not file_name.endswith(TEXT_FILE_EXTENSIONS):
			frappe.throw(
				_("Only plain-text files ({0}) can be read directly; paste other formats into Raw Text.").format(
					", ".join(TEXT_FILE_EXTENSIONS)
				)
			)

		file_doc = frappe.get_doc("File", {"file_url": doc.source_file})
		content = file_doc.get_content()
		if isinstance(content, bytes):
			content = content.decode("utf-8", errors="replace")
		doc.raw_text = content  # what the model saw becomes part of the audit trail
		return content

	if not doc.raw_text:
		frappe.throw(_("Paste the document text into Raw Text before extracting."))
	return doc.raw_text


def _build_hints(doc) -> dict:
	hints = {"company_currency": frappe.get_cached_value("Company", doc.company, "default_currency")}
	if doc.supplier:
		hints["supplier_candidates"] = [
			frappe.get_cached_value("Supplier", doc.supplier, "supplier_name") or doc.supplier
		]
	return hints


def _mark_failed(doc, message: str, raw_output: str | None = None) -> dict:
	"""Provider or parse failure -> status *Failed*, message preserved, no throw."""
	values = {"status": "Failed", "warnings": _("Extraction failed: {0}").format(message)}
	if raw_output is not None:
		values["extraction_json"] = raw_output
	doc.db_set(values)
	frappe.msgprint(_("Extraction failed: {0}").format(message), indicator="red")
	return {"name": doc.name, "status": "Failed", "errors": [message], "warnings": []}


def _apply_extraction(doc, data: dict, errors: list[str], warnings: list[str]) -> None:
	"""Write the validated payload onto the staging document."""
	doc.supplier_name_extracted = data.get("supplier_name")
	doc.supplier_vat = data.get("supplier_vat")
	doc.invoice_number = data.get("invoice_number")
	doc.invoice_date = data.get("invoice_date")
	doc.due_date = data.get("due_date")
	doc.net_total = flt(data.get("net_total"))
	doc.tax_total = flt(data.get("tax_total"))
	doc.grand_total = flt(data.get("grand_total"))

	currency = data.get("currency")
	if currency and not frappe.db.exists("Currency", currency):
		warnings.append(
			_("Currency {0} read from the document is not enabled in this system; set it manually").format(
				currency
			)
		)
		currency = None
	doc.currency = currency

	doc.set("items", [])
	for line in data.get("lines") or []:
		doc.append(
			"items",
			{
				"description": line.get("description"),
				"qty": line.get("qty"),
				"unit_price": line.get("unit_price"),
				"amount": line.get("amount"),
				"tax_rate": line.get("tax_rate"),
			},
		)

	doc.set("taxes", [])
	for tax in data.get("taxes") or []:
		doc.append("taxes", {"rate_pct": tax.get("rate"), "tax_amount": tax.get("amount")})

	needs_review = data.get("needs_review") or []
	doc.needs_review_fields = "\n".join(needs_review)
	doc.warnings = "\n".join(errors + warnings)
	doc.status = "Needs Review" if (errors or needs_review) else "Extracted"


@frappe.whitelist()
def extract_document(name: str) -> dict:
	"""Run the LLM extraction for one staging document.

	Configuration problems (no provider set up, no text to extract) throw —
	they are the operator's to fix before anything runs. Once the provider
	is called, *every* failure lands in status **Failed** with the message
	on the document, never an unhandled exception: this function is
	enqueue-friendly (see :func:`extract_document_async`).
	"""
	doc = frappe.get_doc(STAGING_DOCTYPE, name)
	doc.check_permission("write")

	if doc.status not in EXTRACTABLE_STATUSES:
		frappe.throw(
			_("Extraction can only run from status {0}; {1} is {2}.").format(
				" / ".join(_(status) for status in EXTRACTABLE_STATUSES), doc.name, _(doc.status)
			)
		)

	text = _get_document_text(doc)

	try:
		provider = get_provider()
	except ProviderNotConfigured as e:
		frappe.throw(str(e), title=_("AI Settings Incomplete"))

	user_content = build_user_content(text, _build_hints(doc))

	try:
		raw_output = provider.complete(
			SYSTEM_PROMPT_INVOICE,
			user_content,
			response_json_schema=INVOICE_EXTRACTION_SCHEMA,
			temperature=0.0,
		)
	except ProviderError as e:
		return _mark_failed(doc, str(e))
	except Exception:
		frappe.log_error(title=_("AI extraction failed for {0}").format(doc.name))
		return _mark_failed(doc, _("Unexpected provider error — see the Error Log."))

	threshold = flt(doc.confidence_threshold or 80) / 100.0
	data, errors, warnings = validate_extraction(raw_output, confidence_threshold=threshold)

	doc.provider_used = type(provider).__name__
	doc.model_used = getattr(provider, "model", None)
	doc.extraction_json = raw_output

	if data is None:
		# not JSON at all — keep the raw output for the audit trail
		return _mark_failed(doc, "; ".join(errors), raw_output=raw_output)

	_apply_extraction(doc, data, errors, warnings)
	doc.save()

	return {
		"name": doc.name,
		"status": doc.status,
		"needs_review": data.get("needs_review") or [],
		"errors": errors,
		"warnings": warnings,
	}


@frappe.whitelist()
def extract_document_async(name: str) -> dict:
	"""Enqueue :func:`extract_document` on the long queue (LLM calls are slow)."""
	doc = frappe.get_doc(STAGING_DOCTYPE, name)
	doc.check_permission("write")

	frappe.enqueue(
		"erpnext.ai.api.extract_document",
		queue="long",
		job_id=f"ai_extract::{name}",
		deduplicate=True,
		name=name,
	)
	frappe.msgprint(_("Extraction queued — refresh in a moment."), alert=True)
	return {"name": name, "queued": True}


# -------------------------------------------------------------- resolution


def _resolve_supplier(supplier_name: str | None, vat_id: str | None, warnings: list[str]) -> str | None:
	"""Supplier by VAT (``tax_id``) -> exact name -> record name -> single ``like``.

	Mirror of ``erpnext.edi.inbound.mapper.resolve_supplier`` (which is bound
	to its ``ParsedInvoice`` dataclass): same order, same
	refuse-to-guess-between-two-candidates rule, same warnings.
	"""
	vat_id = (vat_id or "").strip()
	supplier_name = (supplier_name or "").strip()

	if vat_id:
		supplier = frappe.db.get_value("Supplier", {"tax_id": vat_id}, "name")
		if supplier:
			return supplier

	if supplier_name:
		supplier = frappe.db.get_value("Supplier", {"supplier_name": supplier_name}, "name")
		if supplier:
			warnings.append(
				_(
					"Supplier {0} was matched by name, not by VAT ID {1} — set the Tax ID on the "
					"Supplier so future documents match unambiguously"
				).format(supplier, vat_id or _("(none extracted)"))
			)
			return supplier

		if frappe.db.exists("Supplier", supplier_name):
			return supplier_name

		candidates = frappe.get_all(
			"Supplier", filters={"supplier_name": ("like", f"%{supplier_name}%")}, pluck="name", limit=2
		)
		if len(candidates) == 1:
			warnings.append(
				_("Supplier {0} was matched loosely from the name {1}; verify before proceeding").format(
					candidates[0], supplier_name
				)
			)
			return candidates[0]
		if len(candidates) > 1:
			warnings.append(
				_("Name {0} matches several suppliers; select the right one manually").format(supplier_name)
			)

	warnings.append(
		_("No Supplier found for {0} (VAT {1}); select or create one before creating the invoice").format(
			supplier_name or _("(no name extracted)"), vat_id or _("(none)")
		)
	)
	return None


@frappe.whitelist()
def resolve_masters(name: str) -> dict:
	"""Resolve supplier and default accounts on an extracted document.

	* Supplier: VAT then name (see :func:`_resolve_supplier`). Never created.
	* Items: left to the user in v1 — free-text descriptions give no reliable
	  matching basis (no supplier part numbers, unlike structured EDI), and a
	  wrong guessed item books to the wrong expense account and stock ledger.
	* Expense / tax accounts: **reuses** ``erpnext.edi.inbound.mapper``
	  defaulting (Item Default -> company default; purchase tax template ->
	  sole tax account), warnings included.
	"""
	doc = frappe.get_doc(STAGING_DOCTYPE, name)
	doc.check_permission("write")

	if doc.status == "Invoice Created":
		frappe.throw(_("{0} already produced Purchase Invoice {1}").format(doc.name, doc.purchase_invoice))

	warnings: list[str] = []

	if not doc.supplier:
		doc.supplier = _resolve_supplier(doc.supplier_name_extracted, doc.supplier_vat, warnings)

	for row in doc.items:
		if not row.expense_account:
			row.expense_account = resolve_expense_account(row.item_code, doc.company, warnings)

	if doc.taxes and any(not row.account_head for row in doc.taxes):
		tax_account = resolve_tax_account(doc.company, warnings)
		for row in doc.taxes:
			if not row.account_head:
				row.account_head = tax_account

	if warnings:
		doc.warnings = "\n".join(filter(None, [doc.warnings, *warnings]))
	doc.save()

	return {"supplier": doc.supplier, "warnings": warnings}


# ---------------------------------------------------------------- creation


def _assert_no_duplicate_invoice(doc) -> None:
	"""Refuse a second payable for the same supplier invoice number.

	Mirror of ``erpnext.edi.inbound.api._assert_no_duplicate_invoice`` — that
	one is private to the EDI module and keyed to its ``invoice_id``
	fieldname; the rule itself is identical and non-negotiable.
	"""
	existing = frappe.get_all(
		"Purchase Invoice",
		filters={"supplier": doc.supplier, "bill_no": doc.invoice_number, "docstatus": ("<", 2)},
		fields=["name", "docstatus"],
		limit=1,
	)
	if existing:
		state = _("submitted") if existing[0].docstatus == 1 else _("draft")
		frappe.throw(
			_(
				"Purchase Invoice {0} ({1}) already records supplier invoice number {2} for {3}. "
				"Cancel or amend that invoice instead of creating a second one."
			).format(existing[0].name, state, doc.invoice_number, doc.supplier),
			title=_("Duplicate Supplier Invoice"),
		)


def _assert_ready(doc) -> None:
	if doc.status not in CREATABLE_STATUSES:
		frappe.throw(
			_("A Purchase Invoice can only be created from status {0}; {1} is {2}.").format(
				" / ".join(_(status) for status in CREATABLE_STATUSES), doc.name, _(doc.status)
			)
		)
	if doc.purchase_invoice:
		frappe.throw(_("{0} already produced Purchase Invoice {1}").format(doc.name, doc.purchase_invoice))
	if not doc.supplier:
		frappe.throw(_("Set the Supplier before creating a Purchase Invoice"))
	if not doc.invoice_number:
		frappe.throw(_("Set the Supplier Invoice No before creating a Purchase Invoice"))
	if not doc.currency:
		frappe.throw(_("Set the Currency before creating a Purchase Invoice"))
	if not doc.items:
		frappe.throw(_("The document has no lines; nothing to invoice."))

	missing_items = [row.idx for row in doc.items if not row.item_code]
	if missing_items:
		frappe.throw(
			_(
				"Set an Item Code on row(s) {0} — every invoice line needs an item before it can be booked."
			).format(", ".join(str(idx) for idx in missing_items))
		)

	missing_tax_accounts = [row.idx for row in doc.taxes if not row.account_head]
	if missing_tax_accounts:
		frappe.throw(
			_("Set an Account Head on tax row(s) {0}.").format(
				", ".join(str(idx) for idx in missing_tax_accounts)
			)
		)

	_assert_no_duplicate_invoice(doc)


def _build_items(doc, warnings: list[str]) -> list[dict]:
	"""Same total-wins rate reconciliation as ``erpnext.edi.inbound.api``."""
	items = []
	for row in doc.items:
		qty = flt(row.qty) or 1.0
		rate = flt(row.unit_price)
		amount = flt(row.amount)

		# the supplier's line total is the amount we owe; when price x qty
		# disagrees (discounts, rounding), the total wins and we warn
		if amount and abs(rate * qty - amount) > AMOUNT_TOLERANCE:
			derived = amount / qty
			warnings.append(
				_(
					"Row {0}: unit price {1} x quantity {2} is {3}, but the extracted line total is {4}; "
					"the rate was set to {5} so the invoice total matches"
				).format(row.idx, rate, qty, f"{rate * qty:.2f}", f"{amount:.2f}", f"{derived:.4f}")
			)
			rate = derived

		items.append(
			{
				"item_code": row.item_code,
				"description": row.description or row.item_code,
				"qty": qty,
				"uom": row.uom,
				"rate": rate,
				"expense_account": row.expense_account
				or resolve_expense_account(row.item_code, doc.company, warnings),
			}
		)
	return items


def _build_taxes(doc) -> list[dict]:
	# charge_type "Actual" books the exact extracted amount; the rate is kept
	# readable in the description (ERPNext nulls the rate on Actual rows)
	return [
		{
			"charge_type": "Actual",
			"account_head": row.account_head,
			"tax_amount": flt(row.tax_amount),
			"description": _("Tax {0}% (extracted)").format(flt(row.rate_pct)),
			"category": "Total",
			"add_deduct_tax": "Add",
		}
		for row in doc.taxes
	]


@frappe.whitelist()
def create_purchase_invoice(name: str) -> dict:
	"""Create a **draft** Purchase Invoice from a reviewed extraction.

	The draft is never submitted (module docstring). The same gates as the
	structured EDI intake apply: creatable status, resolved supplier and
	items, tax accounts set, and the supplier+bill_no duplicate guard.
	"""
	doc = frappe.get_doc(STAGING_DOCTYPE, name)
	doc.check_permission("write")
	frappe.has_permission("Purchase Invoice", "create", throw=True)

	_assert_ready(doc)

	warnings: list[str] = []
	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": doc.company,
			"supplier": doc.supplier,
			"currency": doc.currency,
			"posting_date": today(),
			"bill_no": doc.invoice_number,
			"bill_date": doc.invoice_date,
			"due_date": doc.due_date,
			"items": _build_items(doc, warnings),
			"taxes": _build_taxes(doc),
		}
	)

	if doc.currency == frappe.get_cached_value("Company", doc.company, "default_currency"):
		pi.conversion_rate = 1.0

	pi.set_missing_values()
	for row in pi.items:
		if not row.conversion_factor:
			row.conversion_factor = 1.0

	pi.insert()  # draft — docstatus 0, never submitted

	doc.db_set({"purchase_invoice": pi.name, "status": "Invoice Created"})
	if warnings:
		doc.db_set("warnings", "\n".join(filter(None, [doc.warnings, *warnings])))

	frappe.msgprint(
		_("Draft Purchase Invoice {0} created — review and submit it manually.").format(
			frappe.utils.get_link_to_form("Purchase Invoice", pi.name)
		),
		indicator="green",
	)

	return {"purchase_invoice": pi.name, "warnings": warnings}
