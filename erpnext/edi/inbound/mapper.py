# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe adapter: :class:`ParsedInvoice` -> **Inbound E-Invoice** payload.

Mirror of ``erpnext.edi.ubl.mapper`` (Sales Invoice -> canonical). This is
the only inbound module besides ``api.py`` that imports frappe, and the only
one that knows ERPNext fieldnames.

Resolution, not creation
------------------------
The Italy-only importer at ``erpnext/regional/doctype/import_supplier_invoice``
creates Suppliers, Addresses, Contacts and UOMs on the fly from whatever the
file says. This mapper deliberately does **not**: a supplier e-invoice is
untrusted third-party input, and silently minting master data from it is how
duplicate supplier records and unusable item masters get into a ledger. Every
lookup that fails leaves the field **blank** and appends a warning; the
staging document is editable so a human completes it once, and the next
invoice from that supplier resolves automatically.

Nothing here writes. :func:`parsed_to_inbound_doc` returns a plain dict plus
the warning list; ``api.import_einvoice_xml`` is what inserts it.
"""

from __future__ import annotations

import frappe
from frappe import _

from erpnext.edi.inbound.models import ParsedInvoice, ParsedLine
from erpnext.edi.ubl.mapper import UOM_CODE_FALLBACK

#: UN/ECE Rec 20 code -> UOM name, the reverse of the outbound fallback map.
#: Built from the outbound table so the two directions cannot drift apart.
UOM_NAME_FALLBACK = {code: name for name, code in reversed(list(UOM_CODE_FALLBACK.items()))}


def _get_uom_by_common_code(unit_code: str) -> str | None:
	"""Reverse of ``erpnext.edi.ubl.mapper._get_common_code`` for UOMs.

	Finds the UOM a genericode-imported ``Common Code`` maps to, so a site
	that configured UN/ECE Rec 20 for outbound gets inbound for free.
	"""
	if not unit_code:
		return None
	codes = frappe.get_all("Common Code", filters={"common_code": unit_code}, pluck="name")
	if not codes:
		return None

	uoms = frappe.get_all(
		"Dynamic Link",
		filters={
			"parenttype": "Common Code",
			"parentfield": "applies_to",
			"parent": ("in", codes),
			"link_doctype": "UOM",
		},
		pluck="link_name",
		order_by="link_name asc",
		limit=1,
	)
	return uoms[0] if uoms else None


def resolve_uom(unit_code: str, warnings: list[str]) -> str | None:
	"""UN/ECE Rec 20 unit code -> UOM name.

	Common Code mapping -> builtin reverse map -> a UOM literally named like
	the code -> Stock Settings default. Only the last two warn.
	"""
	uom = _get_uom_by_common_code(unit_code)
	if uom:
		return uom

	uom = UOM_NAME_FALLBACK.get(unit_code)
	if uom and frappe.db.exists("UOM", uom):
		return uom

	if unit_code and frappe.db.exists("UOM", unit_code):
		return unit_code

	default_uom = frappe.db.get_single_value("Stock Settings", "stock_uom")
	warnings.append(
		_("Unit code {0} could not be mapped to a UOM; defaulted to {1}").format(
			unit_code or _("(empty)"), default_uom or _("(none)")
		)
	)
	return default_uom


def resolve_supplier(parsed: ParsedInvoice, warnings: list[str]) -> tuple[str | None, str | None]:
	"""Find the Supplier for the seller party.

	Order: VAT id (``Supplier.tax_id``) -> exact ``supplier_name`` -> exact
	record name -> a single ``like`` hit. A ``like`` that returns more than
	one candidate resolves to nothing on purpose — guessing between two real
	suppliers is worse than asking.

	Returns ``(supplier, matched_by)``; ``supplier`` is None when unresolved.
	"""
	vat_id = (parsed.seller.vat_id or "").strip()
	name = (parsed.seller.name or "").strip()

	if vat_id:
		supplier = frappe.db.get_value("Supplier", {"tax_id": vat_id}, "name")
		if supplier:
			return supplier, "tax_id"

	if name:
		supplier = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
		if supplier:
			warnings.append(
				_(
					"Supplier {0} was matched by name, not by VAT ID {1} — set the Tax ID on the "
					"Supplier so future invoices match unambiguously"
				).format(supplier, vat_id or _("(none sent)"))
			)
			return supplier, "supplier_name"

		if frappe.db.exists("Supplier", name):
			return name, "name"

		candidates = frappe.get_all(
			"Supplier", filters={"supplier_name": ("like", f"%{name}%")}, pluck="name", limit=2
		)
		if len(candidates) == 1:
			warnings.append(
				_(
					"Supplier {0} was matched loosely from the seller name {1}; verify before proceeding"
				).format(candidates[0], name)
			)
			return candidates[0], "like"
		if len(candidates) > 1:
			warnings.append(
				_("Seller name {0} matches several suppliers; select the right one manually").format(name)
			)

	warnings.append(
		_("No Supplier found for {0} (VAT {1}); select or create one before creating the invoice").format(
			name or _("(no name sent)"), vat_id or _("(none)")
		)
	)
	return None, None


def resolve_item(line: ParsedLine, supplier: str | None, warnings: list[str]) -> str | None:
	"""Find the Item for an invoice line.

	1. ``BuyersItemIdentification`` (BT-156) — the supplier echoed our own
	   item code, so it is checked against ``Item.name`` directly.
	2. ``SellersItemIdentification`` (BT-155) — the supplier's part number,
	   resolved through the ``Item Supplier`` child table
	   (``supplier`` + ``supplier_part_no``), scoped to this supplier.

	Descriptions are deliberately *not* used to guess an item: a wrong item
	code silently books to the wrong expense account and stock ledger.
	"""
	buyer_code = (line.buyer_item_code or "").strip()
	if buyer_code:
		if frappe.db.exists("Item", buyer_code):
			return buyer_code
		warnings.append(
			_("Line {0}: item code {1} sent by the supplier does not exist in Item").format(
				line.line_id, buyer_code
			)
		)

	seller_code = (line.seller_item_code or "").strip()
	if seller_code and supplier:
		items = frappe.get_all(
			"Item Supplier",
			filters={"supplier": supplier, "supplier_part_no": seller_code, "parenttype": "Item"},
			pluck="parent",
			order_by="parent asc",
			limit=2,
		)
		if len(items) == 1:
			return items[0]
		if len(items) > 1:
			warnings.append(
				_("Line {0}: supplier part number {1} is mapped to several items").format(
					line.line_id, seller_code
				)
			)

	warnings.append(
		_(
			"Line {0} ({1}): no item could be resolved — set the Item Code on the row, or record the "
			"supplier part number on the item's Supplier table so it resolves next time"
		).format(line.line_id, line.item_name or line.description or _("no description"))
	)
	return None


def resolve_expense_account(item_code: str | None, company: str, warnings: list[str]) -> str | None:
	"""Item Default for the company -> Company default expense account."""
	if item_code:
		account = frappe.db.get_value(
			"Item Default", {"parent": item_code, "company": company, "parenttype": "Item"}, "expense_account"
		)
		if account:
			return account

	account = frappe.get_cached_value("Company", company, "default_expense_account")
	if account:
		warnings.append(
			_("Line item {0}: expense account defaulted to the company default {1}; verify it").format(
				item_code or _("(unresolved)"), account
			)
		)
		return account

	warnings.append(
		_("No expense account could be resolved for {0}; set one on the item or on the company").format(
			item_code or _("(unresolved item)")
		)
	)
	return None


def resolve_tax_account(company: str, warnings: list[str]) -> str | None:
	"""Account head for the VAT rows of the generated Purchase Invoice.

	Default Purchase Taxes and Charges Template -> any template of the
	company -> a single non-group ``Tax`` account. Every path warns except
	none at all, because booking someone else's VAT to a guessed account is
	exactly the kind of thing an accountant wants to be told about.
	"""
	template = frappe.db.get_value(
		"Purchase Taxes and Charges Template", {"company": company, "is_default": 1, "disabled": 0}, "name"
	) or frappe.db.get_value(
		"Purchase Taxes and Charges Template", {"company": company, "disabled": 0}, "name"
	)

	if template:
		account = frappe.db.get_value(
			"Purchase Taxes and Charges",
			{"parent": template, "parenttype": "Purchase Taxes and Charges Template"},
			"account_head",
			order_by="idx asc",
		)
		if account:
			warnings.append(
				_("Tax account defaulted to {0} from the Purchase Taxes and Charges Template {1}").format(
					account, template
				)
			)
			return account

	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": "Tax", "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name asc",
		limit=2,
	)
	if len(accounts) == 1:
		warnings.append(
			_("Tax account guessed as {0} (the only tax account of the company)").format(accounts[0])
		)
		return accounts[0]

	warnings.append(
		_(
			"No tax account could be resolved for company {0}; set the Account Head on the tax rows "
			"before creating the Purchase Invoice"
		).format(company)
	)
	return None


def parsed_to_inbound_doc(parsed: ParsedInvoice, company: str) -> tuple[dict, list[str]]:
	"""Map a parsed supplier invoice onto an **Inbound E-Invoice** payload.

	Args:
	    parsed: the document as received.
	    company: the receiving company (the buyer side is *not* trusted to
	        identify it — the caller decides which books this lands in).

	Returns:
	    ``(payload, warnings)`` — a dict shaped like the staging doctype and
	    every resolution fallback taken, in the same shape as the outbound
	    mapper's return value. **Nothing is written.**
	"""
	warnings: list[str] = []

	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist").format(company))

	currency = (parsed.currency or "").upper()
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if not currency:
		currency = company_currency
		warnings.append(
			_("The document carries no currency code; assumed the company currency {0}").format(currency)
		)
	elif not frappe.db.exists("Currency", currency):
		warnings.append(
			_("Currency {0} sent by the supplier is not enabled in this system; set it manually").format(
				currency
			)
		)
		currency = None
	elif currency != company_currency:
		warnings.append(
			_(
				"The invoice is in {0} while the company books in {1}; the exchange rate will be fetched"
			).format(currency, company_currency)
		)

	supplier, _matched_by = resolve_supplier(parsed, warnings)

	if parsed.invoice_type_code and parsed.invoice_type_code not in ("380", "381"):
		warnings.append(
			_(
				"Invoice type code {0} is neither 380 (invoice) nor 381 (credit note); review the document"
			).format(parsed.invoice_type_code)
		)
	if parsed.invoice_type_code == "381":
		warnings.append(
			_(
				"The supplier sent a credit note (type 381); the draft is created as a normal Purchase Invoice — mark it as a return manually"
			)
		)

	items = []
	for line in parsed.lines:
		item_code = resolve_item(line, supplier, warnings)
		items.append(
			{
				"line_id": line.line_id,
				"description": line.description or line.item_name,
				"item_name": (line.item_name or line.description or "")[:140],
				"buyer_item_code": line.buyer_item_code,
				"seller_item_code": line.seller_item_code,
				"item_code": item_code,
				"qty": line.qty,
				"uom": resolve_uom(line.unit_code, warnings),
				"unit_price": line.unit_price,
				"line_net": line.line_net,
				"tax_rate": line.tax_rate_pct,
				"expense_account": resolve_expense_account(item_code, company, warnings),
			}
		)

	tax_account = resolve_tax_account(company, warnings) if parsed.taxes else None
	taxes = [
		{
			"category_code": tax.category_code,
			"rate_pct": tax.rate_pct,
			"taxable_amount": tax.taxable_amount,
			"tax_amount": tax.tax_amount,
			"account_head": tax_account,
		}
		for tax in parsed.taxes
	]

	payload = {
		"doctype": "Inbound E-Invoice",
		"company": company,
		"supplier": supplier,
		"supplier_name_parsed": parsed.seller.name,
		"supplier_vat": parsed.seller.vat_id,
		"invoice_id": parsed.invoice_id,
		"issue_date": parsed.issue_date,
		"due_date": parsed.due_date,
		"currency": currency,
		"net_total": parsed.net_total,
		"tax_total": parsed.tax_total,
		"grand_total": parsed.grand_total,
		"raw_profile": parsed.raw_profile,
		"order_reference": parsed.order_reference,
		"status": "Pending Review",
		"items": items,
		"taxes": taxes,
	}

	return payload, warnings
