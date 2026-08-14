# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe adapter: Sales Invoice -> :class:`CanonicalInvoice`.

This is the only UBL module (besides ``api.py``) that imports frappe. It
reads a submitted Sales Invoice and maps it onto the pure canonical model,
resolving standardized codes through the EDI ``Common Code`` substrate
(genericode-imported UN/CEFACT lists) with documented fallbacks.

Currency decision
-----------------
All monetary values are taken in the **invoice (document) currency**:
``items[].net_amount`` / ``items[].rate`` and the header's ``net_total``,
``total_taxes_and_charges``, ``grand_total`` — never the ``base_*`` company
currency twins. The UBL ``DocumentCurrencyCode`` is the Sales Invoice
``currency``; base-currency values would not add up against it.

Every fallback decision (default UOM code, derived taxable base, default
payment means, ...) is collected into the returned ``warnings`` list so the
caller can surface them next to the generated file.
"""

from __future__ import annotations

import frappe
from frappe import _

from erpnext.edi.ubl.models import CanonicalInvoice, InvoiceLine, Party, TaxSubtotal
from erpnext.edi.ubl.profiles import get_profile

#: Common country names -> ISO 3166-1 alpha-2, checked before hitting the
#: Country doctype (whose ``code`` field holds the lower-case ISO code).
COUNTRY_CODE = {
	"Germany": "DE",
	"France": "FR",
	"Italy": "IT",
	"Spain": "ES",
	"Netherlands": "NL",
	"Belgium": "BE",
	"Austria": "AT",
	"Switzerland": "CH",
	"Poland": "PL",
	"Portugal": "PT",
	"Luxembourg": "LU",
	"Ireland": "IE",
	"Denmark": "DK",
	"Sweden": "SE",
	"Norway": "NO",
	"Finland": "FI",
	"United Kingdom": "GB",
	"United States": "US",
	"India": "IN",
	"Malaysia": "MY",
	"Saudi Arabia": "SA",
	"United Arab Emirates": "AE",
	"Singapore": "SG",
	"Australia": "AU",
	"Canada": "CA",
}

#: UOM name -> UN/ECE Recommendation 20 code, used when no Common Code
#: mapping exists for the UOM.
UOM_CODE_FALLBACK = {
	"Nos": "C62",
	"Unit": "C62",
	"Kg": "KGM",
	"Litre": "LTR",
	"Meter": "MTR",
	"Hour": "HUR",
	"Box": "XBX",
	"Set": "SET",
}

DEFAULT_UOM_CODE = "C62"  # UN/ECE "one" (unit)
DEFAULT_PAYMENT_MEANS = "30"  # UNCL4461 credit transfer


def _get_common_code(doctype: str, name: str) -> str | None:
	"""Look up the standardized code mapped to a record via Common Code.

	Joins Common Code to its ``applies_to`` Dynamic Link child rows; returns
	the first mapped ``common_code`` (Common Code validation guarantees one
	mapping per code list, and the genericode importer loads one list per
	concern), or None when the record is unmapped.
	"""
	if not name:
		return None
	codes = frappe.get_all(
		"Common Code",
		filters=[
			["Dynamic Link", "link_doctype", "=", doctype],
			["Dynamic Link", "link_name", "=", name],
		],
		pluck="common_code",
		order_by="common_code asc",
		limit=1,
	)
	return codes[0] if codes else None


def _resolve_country_code(country: str | None, warnings: list[str], context: str) -> str | None:
	if not country:
		return None
	if len(country) == 2 and country.isalpha():
		return country.upper()
	if country in COUNTRY_CODE:
		return COUNTRY_CODE[country]
	code = frappe.get_cached_value("Country", country, "code")
	if code:
		return code.upper()
	warnings.append(_("{0}: could not resolve country {1} to an ISO 3166-1 code").format(context, country))
	return None


def _map_address(address_name: str | None, party: Party, warnings: list[str], context: str) -> None:
	if not address_name:
		warnings.append(_("{0}: no address linked on the Sales Invoice").format(context))
		return
	address = frappe.get_doc("Address", address_name)
	party.street = address.address_line1
	party.additional_street = address.address_line2
	party.city = address.city
	party.postal_code = address.pincode
	code = _resolve_country_code(address.country, warnings, context)
	if code:
		party.country_code = code


def _map_uom(uom: str | None, warnings: list[str]) -> str:
	code = _get_common_code("UOM", uom)
	if code:
		return code
	if uom in UOM_CODE_FALLBACK:
		return UOM_CODE_FALLBACK[uom]
	warnings.append(
		_("UOM {0} has no Common Code mapping; defaulted to {1} ('one'/unit)").format(
			uom or _("(empty)"), DEFAULT_UOM_CODE
		)
	)
	return DEFAULT_UOM_CODE


def _map_payment_means(si, warnings: list[str]) -> str:
	"""UNCL4461 payment means from the first scheduled Mode of Payment."""
	mode_of_payment = None
	for row in si.get("payment_schedule") or []:
		if row.mode_of_payment:
			mode_of_payment = row.mode_of_payment
			break

	if mode_of_payment:
		code = _get_common_code("Mode of Payment", mode_of_payment)
		if code:
			return code
		warnings.append(
			_("Mode of Payment {0} has no Common Code mapping; defaulted to {1} (credit transfer)").format(
				mode_of_payment, DEFAULT_PAYMENT_MEANS
			)
		)
	else:
		warnings.append(
			_(
				"No Mode of Payment on the payment schedule; payment means defaulted to {0} (credit transfer)"
			).format(DEFAULT_PAYMENT_MEANS)
		)
	return DEFAULT_PAYMENT_MEANS


def _map_tax_subtotals(si, warnings: list[str]) -> tuple[list[TaxSubtotal], float]:
	"""Derive VAT category breakdown from Sales Taxes and Charges rows.

	The taxable base per rate is derived arithmetically (tax_amount / rate)
	because header tax rows do not carry a per-rate base; this keeps
	``taxable x rate == tax`` consistent, which EN 16931 checks (BR-CO-17).
	Amounts are in invoice currency (``tax_amount``, not ``base_tax_amount``).
	"""
	subtotals: list[TaxSubtotal] = []
	tax_total = 0.0

	for tax in si.get("taxes") or []:
		if tax.charge_type == "Actual":
			warnings.append(
				_(
					"Tax row {0} ({1}) has charge type 'Actual' and was skipped in the VAT breakdown; review the generated XML"
				).format(tax.idx, tax.description or tax.account_head)
			)
			continue

		rate = float(tax.rate or 0)
		amount = float(tax.tax_amount or 0)
		tax_total += amount

		if rate:
			taxable = amount * 100.0 / rate
			category = "S"
			warnings.append(
				_(
					"Tax row {0}: taxable base {1} derived from tax amount / rate; verify against itemised tax if items carry mixed rates"
				).format(tax.idx, f"{taxable:.2f}")
			)
		else:
			taxable = float(si.net_total or 0)
			category = "Z"
			warnings.append(
				_(
					"Tax row {0} has 0% rate; categorized as 'Z' (zero-rated) — review whether 'E' (exempt) or reverse charge applies"
				).format(tax.idx)
			)

		subtotals.append(
			TaxSubtotal(
				taxable_amount=round(taxable, 2),
				tax_amount=amount,
				category_code=category,
				rate_pct=rate,
			)
		)

	if not subtotals:
		subtotals.append(
			TaxSubtotal(
				taxable_amount=float(si.net_total or 0),
				tax_amount=0.0,
				category_code="Z",
				rate_pct=0.0,
			)
		)
		warnings.append(
			_(
				"Sales Invoice has no tax rows; a single 0% 'Z' (zero-rated) breakdown was assumed — review before transmitting"
			)
		)

	return subtotals, tax_total


def _line_tax_context(si) -> dict:
	"""item row name -> (category_code, rate_pct) from the header tax rows.

	With one effective VAT rate on the document, every line inherits it;
	with several rates the first non-zero rate is used and a caller-visible
	warning is emitted by :func:`_map_lines`.
	"""
	rates = []
	for tax in si.get("taxes") or []:
		if tax.charge_type != "Actual" and tax.rate:
			rates.append(float(tax.rate))
	if rates:
		return {"category": "S", "rate": rates[0], "multiple": len(set(rates)) > 1}
	return {"category": "Z", "rate": 0.0, "multiple": False}


def _map_lines(si, warnings: list[str]) -> list[InvoiceLine]:
	tax_ctx = _line_tax_context(si)
	if tax_ctx["multiple"]:
		warnings.append(
			_(
				"Multiple VAT rates found in the tax table; all lines were classified at {0}% — split invoices per rate or extend the mapper with itemised tax before transmitting"
			).format(f"{tax_ctx['rate']:g}")
		)

	lines = []
	for item in si.items:
		qty = float(item.qty or 0)
		net_amount = float(item.net_amount or 0)  # invoice currency, net of discounts
		unit_price = float(item.net_rate if item.net_rate is not None else item.rate or 0)
		description = frappe.utils.strip_html(item.description or "").strip() or item.item_name
		lines.append(
			InvoiceLine(
				line_id=str(item.idx),
				description=description,
				item_name=item.item_name or item.item_code or description,
				qty=qty,
				unit_code=_map_uom(item.uom or item.stock_uom, warnings),
				unit_price=unit_price,
				line_net=net_amount,
				tax_category_code=tax_ctx["category"],
				tax_rate_pct=tax_ctx["rate"],
			)
		)
	return lines


def sales_invoice_to_canonical(
	sales_invoice_name: str,
	profile_name: str = "peppol-bis-3",
	transmission_profile=None,
) -> tuple[CanonicalInvoice, list[str]]:
	"""Map a Sales Invoice to the canonical UBL model.

	Args:
	    sales_invoice_name: name of the (ideally submitted) Sales Invoice.
	    profile_name: key into :data:`erpnext.edi.ubl.profiles.PROFILES`.
	    transmission_profile: optional ``EDI Transmission Profile`` document
	        (or name) supplying the seller electronic address.

	Returns:
	    (canonical_invoice, warnings) — warnings list every fallback taken.
	"""
	warnings: list[str] = []
	profile = get_profile(profile_name)
	si = frappe.get_doc("Sales Invoice", sales_invoice_name)

	if isinstance(transmission_profile, str):
		transmission_profile = frappe.get_doc("EDI Transmission Profile", transmission_profile)

	company = frappe.get_doc("Company", si.company)

	# --- seller ---
	seller = Party(name=company.company_name or si.company)
	seller.vat_id = si.get("company_tax_id") or company.tax_id
	if not seller.vat_id:
		warnings.append(_("Company {0} has no Tax ID").format(si.company))
	_map_address(si.company_address, seller, warnings, _("Seller address"))
	if not seller.country_code:
		code = _resolve_country_code(company.country, warnings, _("Seller country"))
		if code:
			seller.country_code = code
			warnings.append(_("Seller country taken from Company record (no usable address country)"))
	if transmission_profile and transmission_profile.get("seller_endpoint_id"):
		seller.endpoint_id = transmission_profile.seller_endpoint_id
		seller.endpoint_scheme = transmission_profile.endpoint_scheme_seller

	# --- buyer ---
	buyer = Party(name=si.customer_name or si.customer)
	buyer.vat_id = si.get("tax_id") or frappe.get_cached_value("Customer", si.customer, "tax_id")
	if not buyer.vat_id:
		warnings.append(_("Customer {0} has no Tax ID; buyer VAT omitted").format(si.customer))
	_map_address(si.customer_address, buyer, warnings, _("Buyer address"))

	# --- lines, taxes, payment ---
	lines = _map_lines(si, warnings)
	tax_subtotals, subtotal_sum = _map_tax_subtotals(si, warnings)

	tax_total = float(si.total_taxes_and_charges or 0)
	if abs(subtotal_sum - tax_total) > 0.02:
		warnings.append(
			_("Sum of mapped tax rows ({0}) differs from total taxes and charges ({1})").format(
				f"{subtotal_sum:.2f}", f"{tax_total:.2f}"
			)
		)

	buyer_reference = si.get("po_no") or None
	if buyer_reference:
		warnings.append(
			_(
				"Buyer Reference (BT-10) taken from Customer's Purchase Order No; use a dedicated field for routing IDs (e.g. Leitweg-ID) if required"
			)
		)

	inv = CanonicalInvoice(
		invoice_id=si.name,
		issue_date=str(si.posting_date),
		due_date=str(si.due_date) if si.due_date else None,
		currency=si.currency,
		seller=seller,
		buyer=buyer,
		lines=lines,
		tax_subtotals=tax_subtotals,
		tax_total=tax_total,
		net_total=float(si.net_total or 0),
		grand_total=float(si.grand_total or 0),
		# payable = grand_total in invoice currency; rounded_total /
		# outstanding_amount are deliberately not used (BT-115 is the
		# amount due for payment of *this* document, not the balance)
		payable_amount=float(si.grand_total or 0),
		invoice_type_code="381" if si.get("is_return") else "380",
		payment_means_code=_map_payment_means(si, warnings),
		payment_terms_note=si.get("payment_terms_template") or None,
		buyer_reference=buyer_reference,
		order_reference=si.get("po_no") or None,
		note=si.get("remarks") or None,
	)
	profile.apply(inv)

	return inv, warnings
