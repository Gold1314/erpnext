# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for inbound supplier e-invoice ingestion.

Lifecycle, tracked on the **Inbound E-Invoice** staging document::

    import_einvoice_xml ──► Pending Review ──► Matched ──────► Invoice Created
                                  │  match_against_po  │  create_purchase_invoice
                                  └──► Exception ──────┘  (after the operator
                                                            fixes the rows)

Two safety rules run through every function here and are non-negotiable:

**Never submit.** :func:`create_purchase_invoice` inserts a *draft* Purchase
Invoice and stops. A document a third party generated must be approved by a
human before it posts to the general ledger; automating the submit would let
any party with our e-mail address book entries in our books.

**Never double-book.** The same supplier invoice arriving twice (resend,
mailbox replay, a second import of the same file) must not become two
payables. :func:`create_purchase_invoice` refuses when a draft or submitted
Purchase Invoice already carries the same ``supplier`` + ``bill_no``, naming
the offending document; ERPNext's own duplicate check is a backstop, not the
first line of defence.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, today

from erpnext.edi.inbound.mapper import parsed_to_inbound_doc, resolve_expense_account
from erpnext.edi.inbound.matcher import match_lines, summarize_match
from erpnext.edi.inbound.models import ParsedLine
from erpnext.edi.inbound.parser import ParseError, parse_ubl_invoice

STAGING_DOCTYPE = "Inbound E-Invoice"

#: statuses from which a Purchase Invoice may still be created
CREATABLE_STATUSES = ("Pending Review", "Matched")

#: PO statuses that must never be billed against
CLOSED_PO_STATUSES = ("Closed", "Cancelled", "On Hold")

#: line net vs unit price x quantity: the tolerance below which we keep the
#: supplier's unit price verbatim (EN 16931 allows a cent of rounding)
AMOUNT_TOLERANCE = 0.02


# ------------------------------------------------------------------ import


def _attach_xml(inbound_name: str, file_name: str, content: str):
	"""Attach the raw payload as a private File.

	The XML *as received* is the audit original — everything else on the
	staging document is our interpretation of it. Mirrors the outbound
	``erpnext.edi.ubl.api._attach_xml`` attach pattern.
	"""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": STAGING_DOCTYPE,
			"attached_to_name": inbound_name,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.save()
	return file_doc


def _duplicate_warnings(company: str, supplier: str | None, invoice_id: str) -> list[str]:
	"""Flag — but do not block — an invoice number we have seen before."""
	warnings: list[str] = []
	if not invoice_id:
		return warnings

	filters = {"invoice_id": invoice_id, "company": company}
	if supplier:
		filters["supplier"] = supplier
	seen = frappe.get_all(STAGING_DOCTYPE, filters=filters, pluck="name", limit=3)
	if seen:
		warnings.append(
			_("Invoice number {0} was already imported as {1}").format(invoice_id, ", ".join(seen))
		)

	if supplier:
		existing = frappe.get_all(
			"Purchase Invoice",
			filters={"supplier": supplier, "bill_no": invoice_id, "docstatus": ("<", 2)},
			pluck="name",
			limit=3,
		)
		if existing:
			warnings.append(
				_("Purchase Invoice {0} already carries supplier invoice number {1}").format(
					", ".join(existing), invoice_id
				)
			)

	return warnings


@frappe.whitelist()
def import_einvoice_xml(company: str, xml_content: str, filename: str | None = None) -> dict:
	"""Parse a supplier UBL invoice into an **Inbound E-Invoice** staging doc.

	Args:
	    company: the receiving company. The buyer party in the file is *not*
	        trusted to pick the books an invoice lands in.
	    xml_content: the raw UBL Invoice document.
	    filename: original file name, used for the attachment.

	Returns:
	    ``{"name": ..., "status": ..., "warnings": [...], "errors": [...]}``.

	A document that fails :meth:`ParsedInvoice.validate` is still staged — it
	lands in status *Exception* with the errors listed, because dropping a
	received invoice on the floor is worse than showing an accountant a bad
	one. Only a payload that is not a usable invoice at all (unparseable, no
	invoice number, no lines) is refused outright.
	"""
	frappe.has_permission(STAGING_DOCTYPE, "create", throw=True)

	try:
		parsed = parse_ubl_invoice(xml_content)
	except ParseError as e:
		frappe.throw(str(e), title=_("E-Invoice Could Not Be Read"))

	if not parsed.invoice_id or not parsed.lines:
		frappe.throw(
			_("The document carries no invoice number or no invoice lines and cannot be staged."),
			title=_("Unusable E-Invoice"),
		)

	errors = parsed.validate()
	payload, warnings = parsed_to_inbound_doc(parsed, company)
	warnings = _duplicate_warnings(company, payload.get("supplier"), parsed.invoice_id) + warnings

	if errors:
		payload["status"] = "Exception"
		payload["exceptions"] = "\n".join(errors)
	payload["warnings"] = "\n".join(warnings)

	doc = frappe.get_doc(payload)
	doc.insert()

	file_doc = _attach_xml(doc.name, filename or f"{parsed.invoice_id}.xml", xml_content)
	doc.db_set("source_file", file_doc.file_url)

	return {
		"name": doc.name,
		"status": doc.status,
		"warnings": warnings,
		"errors": errors,
	}


# ------------------------------------------------------------------- match


def _candidate_po_lines(doc) -> tuple[list[dict], list[str]]:
	"""Open Purchase Order lines of this supplier, as matcher input dicts.

	``remaining_qty`` is derived from ``billed_amt`` against the line
	``amount`` — Purchase Order Item tracks billing in money, not quantity,
	so the open quantity is the un-billed *fraction* of the ordered quantity.
	"""
	warnings: list[str] = []
	filters = {
		"supplier": doc.supplier,
		"company": doc.company,
		"docstatus": 1,
		"status": ("not in", CLOSED_PO_STATUSES),
		"per_billed": ("<", 100),
	}

	# cbc:OrderReference (BT-13) usually names the PO outright — when it
	# resolves, match against that order alone instead of everything open
	if doc.get("order_reference"):
		if frappe.db.exists("Purchase Order", {"name": doc.order_reference, "supplier": doc.supplier}):
			filters["name"] = doc.order_reference
		else:
			warnings.append(
				_(
					"The invoice references order {0}, which is not a Purchase Order of {1}; matched against all open orders instead"
				).format(doc.order_reference, doc.supplier)
			)

	orders = frappe.get_all(
		"Purchase Order", filters=filters, pluck="name", order_by="transaction_date asc, name asc"
	)
	if not orders:
		warnings.append(_("No open Purchase Order was found for supplier {0}").format(doc.supplier))
		return [], warnings

	rows = frappe.get_all(
		"Purchase Order Item",
		filters={"parent": ("in", orders), "parenttype": "Purchase Order"},
		fields=[
			"name",
			"parent",
			"idx",
			"item_code",
			"description",
			"qty",
			"rate",
			"amount",
			"billed_amt",
			"received_qty",
		],
		order_by="parent asc, idx asc",
	)

	po_lines = []
	for row in rows:
		amount = flt(row.amount)
		billed = flt(row.billed_amt)
		open_fraction = max(0.0, (amount - billed) / amount) if amount else 1.0
		po_lines.append(
			{
				"item_code": row.item_code,
				"description": frappe.utils.strip_html(row.description or "").strip(),
				"qty": flt(row.qty),
				"rate": flt(row.rate),
				"po_name": row.parent,
				"po_detail": row.name,
				"remaining_qty": flt(row.qty) * open_fraction,
				"idx": row.idx,
				"received_qty": flt(row.received_qty),
			}
		)

	return po_lines, warnings


def _row_to_parsed_line(row) -> ParsedLine:
	"""Rebuild the pure matcher input from a (possibly human-edited) row.

	The resolved ``item_code`` takes precedence over the code the supplier
	sent: once an operator has told us which item a line is, that is the
	strongest evidence available and the exact-match pass should use it.
	"""
	return ParsedLine(
		line_id=row.line_id or str(row.idx),
		description=row.description or "",
		item_name=row.item_name or "",
		qty=flt(row.qty),
		unit_code=row.uom or "",
		unit_price=flt(row.unit_price),
		line_net=flt(row.line_net),
		tax_rate_pct=flt(row.tax_rate),
		buyer_item_code=row.item_code or row.buyer_item_code,
		seller_item_code=row.seller_item_code,
		order_line_ref=row.order_line_ref,
	)


@frappe.whitelist()
def match_against_po(inbound_einvoice: str) -> dict:
	"""Match the staged lines against the supplier's open Purchase Orders.

	Writes ``matched_po`` / ``matched_po_detail`` / ``match_method`` and the
	price and quantity variances onto each row, and the human-readable
	exception list onto the parent. Status becomes *Matched* when every line
	found a PO line inside tolerance, *Exception* otherwise.
	"""
	doc = frappe.get_doc(STAGING_DOCTYPE, inbound_einvoice)
	doc.check_permission("write")

	if doc.status == "Invoice Created":
		frappe.throw(_("{0} already produced Purchase Invoice {1}").format(doc.name, doc.purchase_invoice))
	if not doc.supplier:
		frappe.throw(_("Set the Supplier before matching against Purchase Orders"))

	po_lines, warnings = _candidate_po_lines(doc)
	tolerance = flt(doc.tolerance_pct)
	results = match_lines([_row_to_parsed_line(row) for row in doc.items], po_lines, tolerance)

	for row, result in zip(doc.items, results, strict=True):
		po_line = result.po_line or {}
		row.matched_po = po_line.get("po_name")
		row.matched_po_detail = po_line.get("po_detail")
		row.match_method = result.method
		row.price_variance = result.price_variance_pct
		row.qty_variance = result.qty_variance_pct
		row.within_tolerance = 1 if result.within_tolerance else 0

	all_matched, exceptions = summarize_match(results, tolerance)
	exceptions = warnings + exceptions

	doc.exceptions = "\n".join(exceptions)
	doc.status = "Matched" if all_matched else "Exception"
	doc.save()

	matched_count = sum(1 for result in results if result.matched)
	frappe.msgprint(
		_("{0} of {1} lines matched a Purchase Order line").format(matched_count, len(results)),
		alert=True,
	)

	return {
		"status": doc.status,
		"matched": matched_count,
		"total": len(results),
		"exceptions": exceptions,
	}


# ---------------------------------------------------------------- creation


def _assert_no_duplicate_invoice(doc) -> None:
	"""Refuse to create a second payable for the same supplier invoice."""
	existing = frappe.get_all(
		"Purchase Invoice",
		filters={"supplier": doc.supplier, "bill_no": doc.invoice_id, "docstatus": ("<", 2)},
		fields=["name", "docstatus"],
		limit=1,
	)
	if existing:
		state = _("submitted") if existing[0].docstatus == 1 else _("draft")
		frappe.throw(
			_(
				"Purchase Invoice {0} ({1}) already records supplier invoice number {2} for {3}. "
				"Cancel or amend that invoice instead of creating a second one."
			).format(existing[0].name, state, doc.invoice_id, doc.supplier),
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
	if not doc.currency:
		frappe.throw(_("Set the Currency before creating a Purchase Invoice"))

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
	items = []
	for row in doc.items:
		qty = flt(row.qty)
		rate = flt(row.unit_price)
		line_net = flt(row.line_net)

		# The supplier's line total is the amount we owe. When it disagrees
		# with price x quantity (a line discount, a rounding convention we do
		# not model), the *total* wins and the derived unit rate is warned
		# about — an AP document that does not add up to the supplier's own
		# total is worse than one with an adjusted unit price.
		if qty and abs(rate * qty - line_net) > AMOUNT_TOLERANCE:
			derived = line_net / qty
			warnings.append(
				_(
					"Row {0}: unit price {1} x quantity {2} is {3}, but the supplier's line total is {4}; "
					"the rate was set to {5} so the invoice total matches"
				).format(row.idx, rate, qty, f"{rate * qty:.2f}", f"{line_net:.2f}", f"{derived:.4f}")
			)
			rate = derived

		expense_account = row.expense_account or resolve_expense_account(row.item_code, doc.company, warnings)

		items.append(
			{
				"item_code": row.item_code,
				"item_name": (row.item_name or row.item_code)[:140],
				"description": row.description or row.item_name or row.item_code,
				"qty": qty,
				"uom": row.uom,
				"rate": rate,
				"expense_account": expense_account,
				"purchase_order": row.matched_po,
				"po_detail": row.matched_po_detail,
			}
		)
	return items


def _build_taxes(doc) -> list[dict]:
	# charge_type "Actual" books the exact amount the supplier charged;
	# ERPNext nulls the rate for Actual rows, so the percentage is preserved
	# in the description where a human can still read it.
	return [
		{
			"charge_type": "Actual",
			"account_head": row.account_head,
			"tax_amount": flt(row.tax_amount),
			"description": _("VAT {0}% ({1})").format(flt(row.rate_pct), row.category_code or "S"),
			"category": "Total",
			"add_deduct_tax": "Add",
		}
		for row in doc.taxes
	]


@frappe.whitelist()
def create_purchase_invoice(inbound_einvoice: str) -> dict:
	"""Create a **draft** Purchase Invoice from a staged e-invoice.

	The draft is never submitted (see the module docstring). Where a line was
	matched, its ``purchase_order``/``po_detail`` links are carried over so
	ERPNext's own three-way matching, over-billing checks and PO billing
	percentages take over from here.
	"""
	doc = frappe.get_doc(STAGING_DOCTYPE, inbound_einvoice)
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
			"bill_no": doc.invoice_id,
			"bill_date": doc.issue_date,
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
