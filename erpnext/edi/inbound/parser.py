# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""UBL 2.1 Invoice XML reader.

Pure domain layer: **no frappe imports** — only ``xml.etree.ElementTree``.
Reverse of :mod:`erpnext.edi.ubl.builder`: takes the bytes a supplier sent us
and returns a :class:`~erpnext.edi.inbound.models.ParsedInvoice`.

Namespace handling (the interop rule that matters)
--------------------------------------------------
UBL documents in the wild bind the same three namespaces to wildly different
prefixes — ``cbc:``, ``ns2:``, ``x:``, or no prefix at all when the sender
declares one of them as the default namespace. A prefix is a *serialization
detail chosen by the sender* and carries no meaning; only the namespace URI
identifies an element. Parsers that string-match ``"cbc:ID"`` against the
document text are the single most common cause of "works with vendor A, blows
up with vendor B".

So every lookup here goes through :func:`_find` / :func:`_findtext`, which
take a path written with *our* conventional prefixes and resolve those
prefixes against :data:`NS` — the URI table — before comparing. What is
compared is always ``{namespace-uri}local-name``. Documents that carry no
namespace at all (some ERP exports) are additionally tolerated by matching
the bare local name as a fallback.

Everything optional is optional: a missing element yields ``None`` (or the
type's zero), never an exception. Only a document that is not a UBL Invoice
at all, or that is not well-formed XML, or that carries a non-numeric amount,
raises :class:`ParseError`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from erpnext.edi.inbound.models import ParsedInvoice, ParsedLine, ParsedParty, ParsedTax

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"

#: our conventional prefix -> namespace URI. The prefixes below are used in
#: the *path strings in this module only*; the document may use any prefix.
NS = {
	"inv": NS_INVOICE,
	"cac": NS_CAC,
	"cbc": NS_CBC,
}


class ParseError(Exception):
	"""The payload is not a parseable UBL Invoice."""


def _localname(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
	return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _matches(element: ET.Element, prefix: str, local: str) -> bool:
	"""True when ``element`` is ``local`` in the namespace ``prefix`` maps to.

	The element's own prefix in the source document is irrelevant — by the
	time ElementTree hands us a tag it has already been expanded to
	``{uri}local``. Un-namespaced documents are tolerated.
	"""
	if _localname(element.tag) != local:
		return False
	ns = _namespace(element.tag)
	return ns == NS[prefix] or ns == ""


def _iterfind(parent: ET.Element | None, path: str):
	"""Yield every element under ``parent`` matching a ``cbc:``/``cac:`` path."""
	if parent is None:
		return
	prefix, _, rest = path.partition("/")
	pfx, _, local = prefix.partition(":")
	for child in parent:
		if _matches(child, pfx, local):
			if rest:
				yield from _iterfind(child, rest)
			else:
				yield child


def _find(parent: ET.Element | None, path: str) -> ET.Element | None:
	return next(_iterfind(parent, path), None)


def _findall(parent: ET.Element | None, path: str) -> list[ET.Element]:
	return list(_iterfind(parent, path))


def _findtext(parent: ET.Element | None, path: str) -> str | None:
	"""Stripped text of the first match, or ``None`` when absent/empty."""
	element = _find(parent, path)
	if element is None or element.text is None:
		return None
	text = element.text.strip()
	return text or None


def _to_float(text: str | None, context: str, default: float = 0.0) -> float:
	"""Parse a UBL amount/quantity. Absent -> ``default``; garbage -> ParseError.

	EN 16931 amounts use ``.`` as the decimal mark and no grouping separator.
	Stray whitespace and a leading ``+`` are tolerated; anything else is a
	data error worth surfacing rather than silently zeroing.
	"""
	if text is None:
		return default
	cleaned = text.strip().replace(" ", "").lstrip("+")
	if not cleaned:
		return default
	try:
		return float(cleaned)
	except ValueError:
		raise ParseError(f"{context}: {text!r} is not a valid number") from None


def _amount(parent: ET.Element | None, path: str, context: str, default: float = 0.0) -> float:
	return _to_float(_findtext(parent, path), context, default)


def _parse_party(wrapper: ET.Element | None) -> ParsedParty:
	"""cac:AccountingSupplierParty / cac:AccountingCustomerParty > cac:Party"""
	party = ParsedParty()
	element = _find(wrapper, "cac:Party")
	if element is None:
		return party

	# BT-27/BT-44: PartyName is the trade name; PartyLegalEntity the
	# registered name. Senders populate one or the other (or both).
	party.name = (
		_findtext(element, "cac:PartyName/cbc:Name")
		or _findtext(element, "cac:PartyLegalEntity/cbc:RegistrationName")
		or ""
	)
	party.vat_id = _findtext(element, "cac:PartyTaxScheme/cbc:CompanyID") or _findtext(
		element, "cac:PartyLegalEntity/cbc:CompanyID"
	)
	party.endpoint_id = _findtext(element, "cbc:EndpointID")

	address = _find(element, "cac:PostalAddress")
	if address is not None:
		party.street = _findtext(address, "cbc:StreetName")
		party.city = _findtext(address, "cbc:CityName")
		party.postal_code = _findtext(address, "cbc:PostalZone")
		party.country = _findtext(address, "cac:Country/cbc:IdentificationCode")

	return party


def _parse_line(element: ET.Element) -> ParsedLine:
	line_id = _findtext(element, "cbc:ID") or ""
	context = f"Invoice line {line_id or '?'}"

	quantity = _find(element, "cbc:InvoicedQuantity")
	qty = _to_float(quantity.text if quantity is not None else None, f"{context} quantity")
	unit_code = (quantity.get("unitCode") or "") if quantity is not None else ""

	item = _find(element, "cac:Item")
	tax_category = _find(item, "cac:ClassifiedTaxCategory")

	return ParsedLine(
		line_id=line_id,
		description=_findtext(item, "cbc:Description") or "",
		item_name=_findtext(item, "cbc:Name") or "",
		qty=qty,
		unit_code=unit_code,
		unit_price=_amount(element, "cac:Price/cbc:PriceAmount", f"{context} price"),
		line_net=_amount(element, "cbc:LineExtensionAmount", f"{context} net amount"),
		tax_rate_pct=_amount(tax_category, "cbc:Percent", f"{context} tax percent"),
		tax_category=_findtext(tax_category, "cbc:ID") or "",
		buyer_item_code=_findtext(item, "cac:BuyersItemIdentification/cbc:ID"),
		seller_item_code=_findtext(item, "cac:SellersItemIdentification/cbc:ID"),
		order_line_ref=_findtext(element, "cac:OrderLineReference/cbc:LineID"),
	)


def _parse_taxes(root: ET.Element) -> tuple[list[ParsedTax], float]:
	"""All TaxSubtotal rows across every TaxTotal, plus the summed TaxAmount.

	A document may legitimately carry more than one ``cac:TaxTotal`` (e.g. one
	in the document currency and one in the tax currency); we keep every
	subtotal in document order and add the header amounts, which is correct
	for the single-currency case and visibly wrong (caught by ``validate()``)
	for the dual-currency case rather than silently halved.
	"""
	taxes: list[ParsedTax] = []
	tax_total = 0.0

	for total in _findall(root, "cac:TaxTotal"):
		tax_total += _amount(total, "cbc:TaxAmount", "Tax total")
		for subtotal in _findall(total, "cac:TaxSubtotal"):
			category = _find(subtotal, "cac:TaxCategory")
			taxes.append(
				ParsedTax(
					taxable_amount=_amount(subtotal, "cbc:TaxableAmount", "Tax subtotal taxable amount"),
					tax_amount=_amount(subtotal, "cbc:TaxAmount", "Tax subtotal amount"),
					category_code=_findtext(category, "cbc:ID") or "",
					rate_pct=_amount(category, "cbc:Percent", "Tax subtotal percent"),
				)
			)

	return taxes, tax_total


def parse_ubl_invoice(xml_string: str | bytes) -> ParsedInvoice:
	"""Parse a UBL 2.1 ``<Invoice>`` document into a :class:`ParsedInvoice`.

	Args:
	    xml_string: the raw document, as text or bytes (a BOM and an XML
	        declaration are both fine).

	Raises:
	    ParseError: not well-formed, not an ``Invoice`` root, or an amount
	        that is not a number.
	"""
	if isinstance(xml_string, str):
		payload = xml_string.lstrip("﻿").strip()
	else:
		payload = xml_string

	if not payload:
		raise ParseError("The e-invoice payload is empty")

	try:
		root = ET.fromstring(payload)
	except ET.ParseError as e:
		raise ParseError(f"The e-invoice payload is not well-formed XML: {e}") from None

	local, namespace = _localname(root.tag), _namespace(root.tag)
	if local != "Invoice" or namespace not in (NS_INVOICE, ""):
		if local == "CreditNote" or namespace == NS_CREDIT_NOTE:
			raise ParseError(
				"This is a UBL CreditNote document; only UBL Invoice documents are supported. "
				"Ask the supplier to send a credit note as an Invoice with "
				"InvoiceTypeCode 381, or book it manually."
			)
		raise ParseError(
			f"Expected a UBL Invoice root element, found {root.tag!r}. "
			"Only UBL 2.1 Invoice documents can be imported."
		)

	seller = _parse_party(_find(root, "cac:AccountingSupplierParty"))
	buyer = _parse_party(_find(root, "cac:AccountingCustomerParty"))
	lines = [_parse_line(element) for element in _findall(root, "cac:InvoiceLine")]
	taxes, tax_total = _parse_taxes(root)

	totals = _find(root, "cac:LegalMonetaryTotal")
	# BT-106 (sum of line net amounts); BT-109 (tax exclusive amount) is the
	# fallback because a few senders omit LineExtensionAmount at header level.
	net_total = _amount(totals, "cbc:LineExtensionAmount", "Legal monetary total line extension amount")
	if _find(totals, "cbc:LineExtensionAmount") is None:
		net_total = _amount(totals, "cbc:TaxExclusiveAmount", "Legal monetary total tax exclusive amount")
	grand_total = _amount(totals, "cbc:TaxInclusiveAmount", "Legal monetary total tax inclusive amount")
	payable_amount = _amount(
		totals, "cbc:PayableAmount", "Legal monetary total payable amount", default=grand_total
	)

	return ParsedInvoice(
		invoice_id=_findtext(root, "cbc:ID") or "",
		issue_date=_findtext(root, "cbc:IssueDate"),
		due_date=_findtext(root, "cbc:DueDate"),
		currency=_findtext(root, "cbc:DocumentCurrencyCode") or "",
		invoice_type_code=_findtext(root, "cbc:InvoiceTypeCode") or "",
		seller=seller,
		buyer=buyer,
		lines=lines,
		taxes=taxes,
		tax_total=tax_total,
		net_total=net_total,
		grand_total=grand_total,
		payable_amount=payable_amount,
		order_reference=_findtext(root, "cac:OrderReference/cbc:ID"),
		buyer_reference=_findtext(root, "cbc:BuyerReference"),
		note=_findtext(root, "cbc:Note"),
		raw_profile=_findtext(root, "cbc:CustomizationID") or _findtext(root, "cbc:ProfileID"),
	)
