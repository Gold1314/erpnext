# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""UBL 2.1 Invoice XML serializer.

Pure domain layer: **no frappe imports** — only ``xml.etree.ElementTree``.
Takes a validated :class:`~erpnext.edi.ubl.models.CanonicalInvoice` and emits
a namespaced UBL 2.1 ``<Invoice>`` document with the element order mandated
by the UBL schema (sequence matters: schematron validators reject
out-of-order elements).

Output is deterministic: no timestamps, stable element/attribute order.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

from erpnext.edi.ubl.models import CanonicalInvoice, Party

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

ET.register_namespace("", NS_INVOICE)
ET.register_namespace("cac", NS_CAC)
ET.register_namespace("cbc", NS_CBC)


def _cbc(tag: str) -> str:
	return f"{{{NS_CBC}}}{tag}"


def _cac(tag: str) -> str:
	return f"{{{NS_CAC}}}{tag}"


def _amount(value: float) -> str:
	"""Format a monetary amount with exactly two decimals."""
	return f"{value + 0.0:.2f}"  # +0.0 normalizes -0.0


def _quantity(value: float) -> str:
	"""Format a quantity with up to four decimals, trailing zeros trimmed."""
	text = f"{value:.4f}".rstrip("0").rstrip(".")
	return text or "0"


def _percent(value: float) -> str:
	return _amount(value)


def _text(parent: Element, tag: str, text: str, **attrs: str) -> Element:
	el = SubElement(parent, tag, dict(attrs))
	el.text = text
	return el


def _money(parent: Element, tag: str, value: float, currency: str) -> Element:
	return _text(parent, _cbc(tag), _amount(value), currencyID=currency)


def _build_party(parent: Element, wrapper_tag: str, party: Party) -> None:
	"""cac:AccountingSupplierParty / cac:AccountingCustomerParty > cac:Party"""
	wrapper = SubElement(parent, _cac(wrapper_tag))
	el = SubElement(wrapper, _cac("Party"))

	if party.endpoint_id:
		attrs = {"schemeID": party.endpoint_scheme} if party.endpoint_scheme else {}
		_text(el, _cbc("EndpointID"), party.endpoint_id, **attrs)

	party_name = SubElement(el, _cac("PartyName"))
	_text(party_name, _cbc("Name"), party.name)

	address = SubElement(el, _cac("PostalAddress"))
	if party.street:
		_text(address, _cbc("StreetName"), party.street)
	if party.additional_street:
		_text(address, _cbc("AdditionalStreetName"), party.additional_street)
	if party.city:
		_text(address, _cbc("CityName"), party.city)
	if party.postal_code:
		_text(address, _cbc("PostalZone"), party.postal_code)
	country = SubElement(address, _cac("Country"))
	_text(country, _cbc("IdentificationCode"), party.country_code or "")

	if party.vat_id:
		tax_scheme_wrapper = SubElement(el, _cac("PartyTaxScheme"))
		_text(tax_scheme_wrapper, _cbc("CompanyID"), party.vat_id)
		tax_scheme = SubElement(tax_scheme_wrapper, _cac("TaxScheme"))
		_text(tax_scheme, _cbc("ID"), "VAT")

	legal = SubElement(el, _cac("PartyLegalEntity"))
	_text(legal, _cbc("RegistrationName"), party.name)


def build_invoice_xml(inv: CanonicalInvoice) -> str:
	"""Serialize a canonical invoice to a UBL 2.1 Invoice XML string.

	The caller is responsible for validating first (``inv.validate()`` plus
	profile checks); this function serializes whatever it is given.
	"""
	cur = inv.currency
	root = Element(f"{{{NS_INVOICE}}}Invoice")

	# --- document header, UBL-mandated order ---
	if inv.customization_id:
		_text(root, _cbc("CustomizationID"), inv.customization_id)
	if inv.profile_id:
		_text(root, _cbc("ProfileID"), inv.profile_id)
	_text(root, _cbc("ID"), inv.invoice_id)
	_text(root, _cbc("IssueDate"), inv.issue_date)
	if inv.due_date:
		_text(root, _cbc("DueDate"), inv.due_date)
	_text(root, _cbc("InvoiceTypeCode"), inv.invoice_type_code)
	if inv.note:
		_text(root, _cbc("Note"), inv.note)
	_text(root, _cbc("DocumentCurrencyCode"), cur)
	if inv.buyer_reference:
		_text(root, _cbc("BuyerReference"), inv.buyer_reference)
	if inv.order_reference:
		order_ref = SubElement(root, _cac("OrderReference"))
		_text(order_ref, _cbc("ID"), inv.order_reference)

	# --- parties ---
	_build_party(root, "AccountingSupplierParty", inv.seller)
	_build_party(root, "AccountingCustomerParty", inv.buyer)

	# --- payment ---
	if inv.payment_means_code:
		payment_means = SubElement(root, _cac("PaymentMeans"))
		_text(payment_means, _cbc("PaymentMeansCode"), inv.payment_means_code)
	if inv.payment_terms_note:
		payment_terms = SubElement(root, _cac("PaymentTerms"))
		_text(payment_terms, _cbc("Note"), inv.payment_terms_note)

	# --- tax total ---
	tax_total = SubElement(root, _cac("TaxTotal"))
	_money(tax_total, "TaxAmount", inv.tax_total, cur)
	for sub in inv.tax_subtotals:
		subtotal = SubElement(tax_total, _cac("TaxSubtotal"))
		_money(subtotal, "TaxableAmount", sub.taxable_amount, cur)
		_money(subtotal, "TaxAmount", sub.tax_amount, cur)
		category = SubElement(subtotal, _cac("TaxCategory"))
		_text(category, _cbc("ID"), sub.category_code)
		_text(category, _cbc("Percent"), _percent(sub.rate_pct))
		scheme = SubElement(category, _cac("TaxScheme"))
		_text(scheme, _cbc("ID"), "VAT")

	# --- monetary totals ---
	totals = SubElement(root, _cac("LegalMonetaryTotal"))
	_money(totals, "LineExtensionAmount", inv.net_total, cur)
	_money(totals, "TaxExclusiveAmount", inv.net_total, cur)
	_money(totals, "TaxInclusiveAmount", inv.grand_total, cur)
	_money(totals, "PayableAmount", inv.payable_amount, cur)

	# --- lines ---
	for line in inv.lines:
		line_el = SubElement(root, _cac("InvoiceLine"))
		_text(line_el, _cbc("ID"), line.line_id)
		_text(line_el, _cbc("InvoicedQuantity"), _quantity(line.qty), unitCode=line.unit_code)
		_money(line_el, "LineExtensionAmount", line.line_net, cur)

		item = SubElement(line_el, _cac("Item"))
		if line.description:
			_text(item, _cbc("Description"), line.description)
		_text(item, _cbc("Name"), line.item_name or line.description)
		tax_category = SubElement(item, _cac("ClassifiedTaxCategory"))
		_text(tax_category, _cbc("ID"), line.tax_category_code)
		_text(tax_category, _cbc("Percent"), _percent(line.tax_rate_pct))
		scheme = SubElement(tax_category, _cac("TaxScheme"))
		_text(scheme, _cbc("ID"), "VAT")

		price = SubElement(line_el, _cac("Price"))
		_money(price, "PriceAmount", line.unit_price, cur)

	body = ET.tostring(root, encoding="unicode")
	return '<?xml version="1.0" encoding="UTF-8"?>\n' + body
