# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure inbound e-invoice core.

``models.py``, ``parser.py`` and ``matcher.py`` have no frappe dependency,
but importing them through the ``erpnext`` package would pull in
``erpnext/__init__.py`` (which imports frappe). So when run as a plain file -

	python erpnext/edi/inbound/test_inbound.py

- the modules are loaded directly from their file paths under their canonical
names, keeping ``from erpnext.edi.inbound.models import ...`` working without
a site. The outbound ``ubl`` modules are bootstrapped the same way, because
the headline test here is a **round trip**: build XML with the outbound
builder, parse it back with the inbound parser, assert nothing was lost.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_UBL = _HERE.parent / "ubl"


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


try:
	from erpnext.edi.inbound import matcher, models, parser
	from erpnext.edi.ubl import builder as ubl_builder
	from erpnext.edi.ubl import models as ubl_models
	from erpnext.edi.ubl import profiles as ubl_profiles
except Exception:
	# stand-alone run: register stub packages so the absolute imports inside
	# parser.py / builder.py resolve without importing erpnext/__init__.py
	# (which needs frappe)
	for _pkg in ("erpnext", "erpnext.edi", "erpnext.edi.ubl", "erpnext.edi.inbound"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	ubl_models = _load_module("erpnext.edi.ubl.models", _UBL / "models.py")
	ubl_profiles = _load_module("erpnext.edi.ubl.profiles", _UBL / "profiles.py")
	ubl_builder = _load_module("erpnext.edi.ubl.builder", _UBL / "builder.py")

	models = _load_module("erpnext.edi.inbound.models", _HERE / "models.py")
	parser = _load_module("erpnext.edi.inbound.parser", _HERE / "parser.py")
	matcher = _load_module("erpnext.edi.inbound.matcher", _HERE / "matcher.py")


# ---------------------------------------------------------------- fixtures


def make_outbound_fixture() -> ubl_models.CanonicalInvoice:
	"""Two lines at two tax rates (19% / 7%), EUR, all cross-sums consistent.

	Mirrors ``erpnext/edi/ubl/test_ubl.py``'s fixture, including the awkward
	characters, so the round trip also proves XML escaping survives.
	"""
	seller = ubl_models.Party(
		name="Muster & Söhne <GmbH>",
		vat_id="DE123456789",
		street="Hauptstraße 1",
		additional_street="Gebäude B",
		city="Berlin",
		postal_code="10115",
		country_code="DE",
		endpoint_id="4012345000009",
		endpoint_scheme="0088",
	)
	buyer = ubl_models.Party(
		name="Client & Co SARL",
		vat_id="FR40303265045",
		street="1 rue de la Paix",
		city="Paris",
		postal_code="75002",
		country_code="FR",
		endpoint_id="FR40303265045",
		endpoint_scheme="9957",
	)
	lines = [
		ubl_models.InvoiceLine(
			line_id="1",
			description="Widget, blue <deluxe>",
			item_name="Widget",
			qty=2,
			unit_code="C62",
			unit_price=100.0,
			line_net=200.0,
			tax_category_code="S",
			tax_rate_pct=19.0,
		),
		ubl_models.InvoiceLine(
			line_id="2",
			description="Grain, milled",
			item_name="Grain",
			qty=5.5,
			unit_code="KGM",
			unit_price=10.0,
			line_net=55.0,
			tax_category_code="S",
			tax_rate_pct=7.0,
		),
	]
	subtotals = [
		ubl_models.TaxSubtotal(taxable_amount=200.0, tax_amount=38.0, category_code="S", rate_pct=19.0),
		ubl_models.TaxSubtotal(taxable_amount=55.0, tax_amount=3.85, category_code="S", rate_pct=7.0),
	]
	inv = ubl_models.CanonicalInvoice(
		invoice_id="SUPP-INV-2026-00042",
		issue_date="2026-08-13",
		due_date="2026-09-12",
		currency="EUR",
		seller=seller,
		buyer=buyer,
		lines=lines,
		tax_subtotals=subtotals,
		tax_total=41.85,
		net_total=255.0,
		grand_total=296.85,
		payable_amount=296.85,
		payment_means_code="30",
		payment_terms_note="Net 30 days",
		buyer_reference="04011000-1234512345-06",
		order_reference="PUR-ORD-2026-00007",
		note="Delivery per agreement & incoterms <EXW>",
	)
	ubl_profiles.get_profile("peppol-bis-3").apply(inv)
	return inv


#: A hand-written document using deliberately hostile namespace prefixes:
#: the UBL namespaces are bound to ``ns0``/``x``/``q1`` and the Invoice
#: namespace is the *default* namespace. Every URI is correct; every prefix
#: is wrong-looking. A prefix-matching parser fails this outright.
WEIRD_PREFIX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:q1="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:ns0="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <ns0:CustomizationID>urn:cen.eu:en16931:2017</ns0:CustomizationID>
  <ns0:ID>WEIRD-1</ns0:ID>
  <ns0:IssueDate>2026-03-01</ns0:IssueDate>
  <ns0:DueDate>2026-03-31</ns0:DueDate>
  <ns0:InvoiceTypeCode>380</ns0:InvoiceTypeCode>
  <ns0:DocumentCurrencyCode>SEK</ns0:DocumentCurrencyCode>
  <ns0:BuyerReference>COST-CENTRE-9</ns0:BuyerReference>
  <q1:OrderReference><ns0:ID>PUR-ORD-2026-00099</ns0:ID></q1:OrderReference>
  <q1:AccountingSupplierParty>
    <q1:Party>
      <ns0:EndpointID schemeID="0007">5567890123</ns0:EndpointID>
      <q1:PartyName><ns0:Name>Nordic Parts AB</ns0:Name></q1:PartyName>
      <q1:PostalAddress>
        <ns0:StreetName>Storgatan 5</ns0:StreetName>
        <ns0:CityName>Stockholm</ns0:CityName>
        <ns0:PostalZone>11122</ns0:PostalZone>
        <q1:Country><ns0:IdentificationCode>SE</ns0:IdentificationCode></q1:Country>
      </q1:PostalAddress>
      <q1:PartyTaxScheme>
        <ns0:CompanyID>SE556789012301</ns0:CompanyID>
        <q1:TaxScheme><ns0:ID>VAT</ns0:ID></q1:TaxScheme>
      </q1:PartyTaxScheme>
    </q1:Party>
  </q1:AccountingSupplierParty>
  <q1:AccountingCustomerParty>
    <q1:Party>
      <q1:PartyLegalEntity><ns0:RegistrationName>Our Company Ltd</ns0:RegistrationName></q1:PartyLegalEntity>
    </q1:Party>
  </q1:AccountingCustomerParty>
  <q1:TaxTotal>
    <ns0:TaxAmount currencyID="SEK">25.00</ns0:TaxAmount>
    <q1:TaxSubtotal>
      <ns0:TaxableAmount currencyID="SEK">100.00</ns0:TaxableAmount>
      <ns0:TaxAmount currencyID="SEK">25.00</ns0:TaxAmount>
      <q1:TaxCategory>
        <ns0:ID>S</ns0:ID>
        <ns0:Percent>25.00</ns0:Percent>
      </q1:TaxCategory>
    </q1:TaxSubtotal>
  </q1:TaxTotal>
  <q1:LegalMonetaryTotal>
    <ns0:LineExtensionAmount currencyID="SEK">100.00</ns0:LineExtensionAmount>
    <ns0:TaxExclusiveAmount currencyID="SEK">100.00</ns0:TaxExclusiveAmount>
    <ns0:TaxInclusiveAmount currencyID="SEK">125.00</ns0:TaxInclusiveAmount>
    <ns0:PayableAmount currencyID="SEK">125.00</ns0:PayableAmount>
  </q1:LegalMonetaryTotal>
  <q1:InvoiceLine>
    <ns0:ID>10</ns0:ID>
    <ns0:InvoicedQuantity unitCode="KGM">4</ns0:InvoicedQuantity>
    <ns0:LineExtensionAmount currencyID="SEK">100.00</ns0:LineExtensionAmount>
    <q1:OrderLineReference><ns0:LineID>3</ns0:LineID></q1:OrderLineReference>
    <q1:Item>
      <ns0:Description>Steel bolt M8, galvanised</ns0:Description>
      <ns0:Name>Steel bolt</ns0:Name>
      <q1:BuyersItemIdentification><ns0:ID>BOLT-M8</ns0:ID></q1:BuyersItemIdentification>
      <q1:SellersItemIdentification><ns0:ID>NP-88-421</ns0:ID></q1:SellersItemIdentification>
      <q1:ClassifiedTaxCategory>
        <ns0:ID>S</ns0:ID>
        <ns0:Percent>25.00</ns0:Percent>
      </q1:ClassifiedTaxCategory>
    </q1:Item>
    <q1:Price><ns0:PriceAmount currencyID="SEK">25.00</ns0:PriceAmount></q1:Price>
  </q1:InvoiceLine>
</Invoice>
"""

#: The bare minimum a document can carry and still be an Invoice: no dates,
#: no due date, no parties beyond a name, no taxes, no prices, no totals.
MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>MIN-1</cbc:ID>
  <cbc:IssueDate>2026-01-02</cbc:IssueDate>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cac:Item><cbc:Name>Something</cbc:Name></cac:Item>
  </cac:InvoiceLine>
</Invoice>
"""


def po_line(**kwargs) -> dict:
	base = {
		"item_code": None,
		"description": "",
		"qty": 1.0,
		"rate": 1.0,
		"po_name": "PUR-ORD-2026-00007",
		"po_detail": "row-x",
		"remaining_qty": 1.0,
	}
	base.update(kwargs)
	return base


def parsed_line(**kwargs) -> models.ParsedLine:
	base = {
		"line_id": "1",
		"description": "",
		"item_name": "",
		"qty": 1.0,
		"unit_code": "C62",
		"unit_price": 1.0,
		"line_net": 1.0,
	}
	base.update(kwargs)
	return models.ParsedLine(**base)


# ------------------------------------------------------------ round trip


class TestRoundTrip(unittest.TestCase):
	"""Build with the outbound builder, read back with the inbound parser.

	This is the cheapest strong correctness proof available: the two sides
	were written independently against the UBL spec, so agreement on every
	field is real evidence rather than a restatement of one implementation.
	"""

	@classmethod
	def setUpClass(cls):
		cls.outbound = make_outbound_fixture()
		cls.xml = ubl_builder.build_invoice_xml(cls.outbound)
		cls.parsed = parser.parse_ubl_invoice(cls.xml)

	def test_header_survives(self):
		out, back = self.outbound, self.parsed
		self.assertEqual(back.invoice_id, out.invoice_id)
		self.assertEqual(back.issue_date, out.issue_date)
		self.assertEqual(back.due_date, out.due_date)
		self.assertEqual(back.currency, out.currency)
		self.assertEqual(back.invoice_type_code, out.invoice_type_code)
		self.assertEqual(back.buyer_reference, out.buyer_reference)
		self.assertEqual(back.order_reference, out.order_reference)
		self.assertEqual(back.note, out.note)
		self.assertEqual(back.raw_profile, out.customization_id)

	def test_parties_survive(self):
		out, back = self.outbound, self.parsed
		for role in ("seller", "buyer"):
			sent, received = getattr(out, role), getattr(back, role)
			with self.subTest(role=role):
				self.assertEqual(received.name, sent.name)
				self.assertEqual(received.vat_id, sent.vat_id)
				self.assertEqual(received.endpoint_id, sent.endpoint_id)
				self.assertEqual(received.country, sent.country_code)
				self.assertEqual(received.street, sent.street)
				self.assertEqual(received.city, sent.city)
				self.assertEqual(received.postal_code, sent.postal_code)

	def test_special_characters_survive(self):
		# escaped on the way out, unescaped on the way back in
		self.assertIn("Muster &amp; Söhne &lt;GmbH&gt;", self.xml)
		self.assertEqual(self.parsed.seller.name, "Muster & Söhne <GmbH>")
		self.assertEqual(self.parsed.lines[0].description, "Widget, blue <deluxe>")
		self.assertEqual(self.parsed.note, "Delivery per agreement & incoterms <EXW>")

	def test_lines_survive(self):
		self.assertEqual(len(self.parsed.lines), len(self.outbound.lines))
		for sent, received in zip(self.outbound.lines, self.parsed.lines, strict=True):
			with self.subTest(line=sent.line_id):
				self.assertEqual(received.line_id, sent.line_id)
				self.assertEqual(received.description, sent.description)
				self.assertEqual(received.item_name, sent.item_name)
				self.assertAlmostEqual(received.qty, sent.qty, places=4)
				self.assertEqual(received.unit_code, sent.unit_code)
				self.assertAlmostEqual(received.unit_price, sent.unit_price, places=2)
				self.assertAlmostEqual(received.line_net, sent.line_net, places=2)
				self.assertEqual(received.tax_category, sent.tax_category_code)
				self.assertAlmostEqual(received.tax_rate_pct, sent.tax_rate_pct, places=2)

	def test_taxes_and_totals_survive(self):
		out, back = self.outbound, self.parsed
		self.assertAlmostEqual(back.tax_total, out.tax_total, places=2)
		self.assertAlmostEqual(back.net_total, out.net_total, places=2)
		self.assertAlmostEqual(back.grand_total, out.grand_total, places=2)
		self.assertAlmostEqual(back.payable_amount, out.payable_amount, places=2)

		self.assertEqual(len(back.taxes), len(out.tax_subtotals))
		for sent, received in zip(out.tax_subtotals, back.taxes, strict=True):
			with self.subTest(rate=sent.rate_pct):
				self.assertAlmostEqual(received.taxable_amount, sent.taxable_amount, places=2)
				self.assertAlmostEqual(received.tax_amount, sent.tax_amount, places=2)
				self.assertEqual(received.category_code, sent.category_code)
				self.assertAlmostEqual(received.rate_pct, sent.rate_pct, places=2)

	def test_round_tripped_document_validates(self):
		self.assertEqual(self.parsed.validate(), [])

	def test_fields_the_builder_does_not_emit_are_none(self):
		# the outbound builder carries no item cross-references; the parser
		# must report their absence rather than inventing a value
		for line in self.parsed.lines:
			self.assertIsNone(line.buyer_item_code)
			self.assertIsNone(line.seller_item_code)
			self.assertIsNone(line.order_line_ref)

	def test_round_trip_is_stable_across_two_hops(self):
		# parse -> rebuild -> parse must be a fixed point
		rebuilt = ubl_models.CanonicalInvoice(
			invoice_id=self.parsed.invoice_id,
			issue_date=self.parsed.issue_date,
			due_date=self.parsed.due_date,
			currency=self.parsed.currency,
			seller=ubl_models.Party(
				name=self.parsed.seller.name,
				vat_id=self.parsed.seller.vat_id,
				street=self.parsed.seller.street,
				city=self.parsed.seller.city,
				postal_code=self.parsed.seller.postal_code,
				country_code=self.parsed.seller.country,
				endpoint_id=self.parsed.seller.endpoint_id,
				endpoint_scheme="0088",
			),
			buyer=ubl_models.Party(name=self.parsed.buyer.name, country_code=self.parsed.buyer.country),
			lines=[
				ubl_models.InvoiceLine(
					line_id=line.line_id,
					description=line.description,
					item_name=line.item_name,
					qty=line.qty,
					unit_code=line.unit_code,
					unit_price=line.unit_price,
					line_net=line.line_net,
					tax_category_code=line.tax_category,
					tax_rate_pct=line.tax_rate_pct,
				)
				for line in self.parsed.lines
			],
			tax_subtotals=[
				ubl_models.TaxSubtotal(
					taxable_amount=tax.taxable_amount,
					tax_amount=tax.tax_amount,
					category_code=tax.category_code,
					rate_pct=tax.rate_pct,
				)
				for tax in self.parsed.taxes
			],
			tax_total=self.parsed.tax_total,
			net_total=self.parsed.net_total,
			grand_total=self.parsed.grand_total,
			payable_amount=self.parsed.payable_amount,
			invoice_type_code=self.parsed.invoice_type_code,
			buyer_reference=self.parsed.buyer_reference,
			order_reference=self.parsed.order_reference,
			note=self.parsed.note,
		)
		again = parser.parse_ubl_invoice(ubl_builder.build_invoice_xml(rebuilt))
		self.assertEqual(again.lines, self.parsed.lines)
		self.assertEqual(again.taxes, self.parsed.taxes)
		self.assertEqual(again.seller, self.parsed.seller)


# ---------------------------------------------------------------- parser


class TestNamespaceResolution(unittest.TestCase):
	"""Prefixes are the sender's business; only the URI identifies elements."""

	def setUp(self):
		self.parsed = parser.parse_ubl_invoice(WEIRD_PREFIX_XML)

	def test_header_with_unusual_prefixes(self):
		self.assertEqual(self.parsed.invoice_id, "WEIRD-1")
		self.assertEqual(self.parsed.issue_date, "2026-03-01")
		self.assertEqual(self.parsed.due_date, "2026-03-31")
		self.assertEqual(self.parsed.currency, "SEK")
		self.assertEqual(self.parsed.invoice_type_code, "380")
		self.assertEqual(self.parsed.buyer_reference, "COST-CENTRE-9")
		self.assertEqual(self.parsed.order_reference, "PUR-ORD-2026-00099")
		self.assertEqual(self.parsed.raw_profile, "urn:cen.eu:en16931:2017")

	def test_parties_with_unusual_prefixes(self):
		seller = self.parsed.seller
		self.assertEqual(seller.name, "Nordic Parts AB")
		self.assertEqual(seller.vat_id, "SE556789012301")
		self.assertEqual(seller.endpoint_id, "5567890123")
		self.assertEqual(seller.street, "Storgatan 5")
		self.assertEqual(seller.city, "Stockholm")
		self.assertEqual(seller.postal_code, "11122")
		self.assertEqual(seller.country, "SE")
		# buyer name falls back to the legal entity registration name
		self.assertEqual(self.parsed.buyer.name, "Our Company Ltd")

	def test_lines_with_unusual_prefixes(self):
		(line,) = self.parsed.lines
		self.assertEqual(line.line_id, "10")
		self.assertEqual(line.qty, 4.0)
		self.assertEqual(line.unit_code, "KGM")
		self.assertEqual(line.unit_price, 25.0)
		self.assertEqual(line.line_net, 100.0)
		self.assertEqual(line.item_name, "Steel bolt")
		self.assertEqual(line.description, "Steel bolt M8, galvanised")
		self.assertEqual(line.buyer_item_code, "BOLT-M8")
		self.assertEqual(line.seller_item_code, "NP-88-421")
		self.assertEqual(line.order_line_ref, "3")
		self.assertEqual(line.tax_category, "S")
		self.assertEqual(line.tax_rate_pct, 25.0)

	def test_totals_with_unusual_prefixes(self):
		self.assertEqual(self.parsed.net_total, 100.0)
		self.assertEqual(self.parsed.tax_total, 25.0)
		self.assertEqual(self.parsed.grand_total, 125.0)
		self.assertEqual(self.parsed.payable_amount, 125.0)
		(tax,) = self.parsed.taxes
		self.assertEqual(
			(tax.taxable_amount, tax.tax_amount, tax.category_code, tax.rate_pct), (100.0, 25.0, "S", 25.0)
		)
		self.assertEqual(self.parsed.validate(), [])

	def test_same_document_reserialized_with_other_prefixes_parses_identically(self):
		swapped = (
			WEIRD_PREFIX_XML.replace("ns0:", "cbc:")
			.replace("xmlns:ns0", "xmlns:cbc")
			.replace("q1:", "zzz:")
			.replace("xmlns:q1", "xmlns:zzz")
		)
		self.assertEqual(parser.parse_ubl_invoice(swapped), self.parsed)

	def test_foreign_namespace_with_the_same_local_name_is_ignored(self):
		hijacked = WEIRD_PREFIX_XML.replace(
			"<ns0:ID>WEIRD-1</ns0:ID>",
			'<ext:ID xmlns:ext="urn:example:extension">HIJACKED</ext:ID><ns0:ID>WEIRD-1</ns0:ID>',
		)
		self.assertEqual(parser.parse_ubl_invoice(hijacked).invoice_id, "WEIRD-1")


class TestTolerance(unittest.TestCase):
	def test_minimal_document_parses(self):
		parsed = parser.parse_ubl_invoice(MINIMAL_XML)
		self.assertEqual(parsed.invoice_id, "MIN-1")
		self.assertEqual(parsed.issue_date, "2026-01-02")
		self.assertIsNone(parsed.due_date)
		self.assertIsNone(parsed.note)
		self.assertIsNone(parsed.order_reference)
		self.assertIsNone(parsed.buyer_reference)
		self.assertIsNone(parsed.raw_profile)
		self.assertEqual(parsed.currency, "")
		self.assertEqual(parsed.invoice_type_code, "")
		self.assertEqual(parsed.seller, models.ParsedParty())
		self.assertEqual(parsed.taxes, [])
		self.assertEqual((parsed.net_total, parsed.tax_total, parsed.grand_total), (0.0, 0.0, 0.0))

	def test_minimal_document_line_defaults(self):
		(line,) = parser.parse_ubl_invoice(MINIMAL_XML).lines
		self.assertEqual(line.line_id, "1")
		self.assertEqual(line.item_name, "Something")
		self.assertEqual(line.description, "")
		self.assertEqual((line.qty, line.unit_price, line.line_net), (0.0, 0.0, 0.0))
		self.assertEqual(line.unit_code, "")
		self.assertIsNone(line.buyer_item_code)

	def test_payable_amount_defaults_to_grand_total(self):
		xml = WEIRD_PREFIX_XML.replace('<ns0:PayableAmount currencyID="SEK">125.00</ns0:PayableAmount>', "")
		self.assertEqual(parser.parse_ubl_invoice(xml).payable_amount, 125.0)

	def test_net_total_falls_back_to_tax_exclusive_amount(self):
		xml = WEIRD_PREFIX_XML.replace(
			'<ns0:LineExtensionAmount currencyID="SEK">100.00</ns0:LineExtensionAmount>\n'
			"    <ns0:TaxExclusiveAmount",
			"<ns0:TaxExclusiveAmount",
		)
		self.assertEqual(parser.parse_ubl_invoice(xml).net_total, 100.0)

	def test_bom_and_bytes_input(self):
		self.assertEqual(parser.parse_ubl_invoice("﻿" + MINIMAL_XML).invoice_id, "MIN-1")
		self.assertEqual(parser.parse_ubl_invoice(MINIMAL_XML.encode("utf-8")).invoice_id, "MIN-1")

	def test_undeclared_namespace_document_is_tolerated(self):
		bare = (
			MINIMAL_XML.replace(
				'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"\n'
				'         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"\n'
				'         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">',
				"<Invoice>",
			)
			.replace("cbc:", "")
			.replace("cac:", "")
		)
		self.assertEqual(parser.parse_ubl_invoice(bare).invoice_id, "MIN-1")


class TestParseErrors(unittest.TestCase):
	def test_garbage_is_rejected(self):
		with self.assertRaises(parser.ParseError) as ctx:
			parser.parse_ubl_invoice("this is a PDF, not XML")
		self.assertIn("well-formed", str(ctx.exception))

	def test_empty_payload_is_rejected(self):
		with self.assertRaises(parser.ParseError):
			parser.parse_ubl_invoice("   ")

	def test_wrong_root_is_rejected(self):
		with self.assertRaises(parser.ParseError) as ctx:
			parser.parse_ubl_invoice("<Order><ID>1</ID></Order>")
		self.assertIn("UBL Invoice root", str(ctx.exception))

	def test_credit_note_gets_a_specific_message(self):
		xml = '<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"></CreditNote>'
		with self.assertRaises(parser.ParseError) as ctx:
			parser.parse_ubl_invoice(xml)
		self.assertIn("CreditNote", str(ctx.exception))

	def test_non_numeric_amount_is_rejected_with_context(self):
		xml = WEIRD_PREFIX_XML.replace(
			'<ns0:TaxInclusiveAmount currencyID="SEK">125.00</ns0:TaxInclusiveAmount>',
			'<ns0:TaxInclusiveAmount currencyID="SEK">one hundred</ns0:TaxInclusiveAmount>',
		)
		with self.assertRaises(parser.ParseError) as ctx:
			parser.parse_ubl_invoice(xml)
		self.assertIn("tax inclusive amount", str(ctx.exception).lower())


class TestParsedInvoiceValidation(unittest.TestCase):
	def _fixture(self):
		return parser.parse_ubl_invoice(ubl_builder.build_invoice_xml(make_outbound_fixture()))

	def test_valid_fixture(self):
		self.assertEqual(self._fixture().validate(), [])

	def test_line_sum_mismatch(self):
		inv = self._fixture()
		inv.lines[0].line_net = 100.0  # 255.00 -> 155.00
		errors = inv.validate()
		self.assertTrue(any("Sum of line net amounts" in e for e in errors), errors)

	def test_grand_total_mismatch(self):
		inv = self._fixture()
		inv.grand_total = 999.0
		errors = inv.validate()
		self.assertTrue(any("grand total" in e for e in errors), errors)

	def test_tolerance_of_one_cent_each_side(self):
		inv = self._fixture()
		inv.net_total += 0.01
		inv.grand_total += 0.01
		self.assertEqual(inv.validate(), [])
		inv.net_total += 0.02
		self.assertTrue(inv.validate())

	def test_tax_subtotal_sum_mismatch(self):
		inv = self._fixture()
		inv.taxes[0].tax_amount = 1.0
		errors = inv.validate()
		self.assertTrue(any("Sum of tax subtotal" in e for e in errors), errors)

	def test_missing_seller_vat_and_id(self):
		inv = self._fixture()
		inv.seller.vat_id = None
		inv.invoice_id = ""
		errors = inv.validate()
		self.assertTrue(any("Seller VAT" in e for e in errors), errors)
		self.assertTrue(any("Invoice ID" in e for e in errors), errors)

	def test_missing_dates_and_currency(self):
		inv = self._fixture()
		inv.issue_date = None
		inv.currency = "EURO"
		errors = inv.validate()
		self.assertTrue(any("Issue date" in e for e in errors), errors)
		self.assertTrue(any("ISO 4217" in e for e in errors), errors)

	def test_empty_invoice_is_invalid(self):
		inv = self._fixture()
		inv.lines = []
		self.assertTrue(any("no lines" in e for e in inv.validate()))


# --------------------------------------------------------------- matcher


class TestMatcher(unittest.TestCase):
	def test_exact_item_code_match_wins_and_ignores_order(self):
		lines = [parsed_line(line_id="1", buyer_item_code="WIDGET-B", qty=2, unit_price=100.0)]
		pos = [
			po_line(item_code="OTHER", description="Something else", qty=2, rate=100.0, po_detail="r1"),
			po_line(item_code="WIDGET-B", description="Widget blue", qty=2, rate=100.0, po_detail="r2"),
		]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.method, matcher.METHOD_ITEM_CODE)
		self.assertEqual(result.po_line["po_detail"], "r2")
		self.assertTrue(result.within_tolerance)

	def test_order_line_ref_match(self):
		lines = [parsed_line(line_id="1", order_line_ref="2", qty=3, unit_price=7.0)]
		pos = [
			po_line(item_code="A", qty=3, rate=7.0, po_detail="r1"),
			po_line(item_code="B", qty=3, rate=7.0, po_detail="r2"),
		]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.method, matcher.METHOD_ORDER_LINE_REF)
		self.assertEqual(result.po_line["po_detail"], "r2")

	def test_order_line_ref_uses_explicit_idx_when_present(self):
		lines = [parsed_line(line_id="1", order_line_ref="7", qty=1, unit_price=1.0)]
		pos = [po_line(item_code="A", po_detail="r1", idx=7)]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.method, matcher.METHOD_ORDER_LINE_REF)

	def test_fuzzy_description_match_above_threshold(self):
		lines = [parsed_line(line_id="1", description="Widget, blue (deluxe)", qty=1, unit_price=5.0)]
		pos = [po_line(item_code="W-1", description="widget blue deluxe", qty=1, rate=5.0, po_detail="r1")]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.method, matcher.METHOD_DESCRIPTION)
		self.assertGreaterEqual(matcher.similarity("Widget, blue (deluxe)", "widget blue deluxe"), 0.85)

	def test_description_just_below_threshold_stays_unmatched(self):
		self.assertLess(matcher.similarity("Steel bolt M8 galvanised", "Brass nut M6 plain"), 0.85)
		lines = [parsed_line(line_id="1", description="Steel bolt M8 galvanised")]
		pos = [po_line(item_code="N-6", description="Brass nut M6 plain")]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.method, matcher.METHOD_UNMATCHED)
		self.assertIsNone(result.po_line)
		self.assertFalse(result.within_tolerance)

	def test_unmatched_when_no_po_lines(self):
		results = matcher.match_lines([parsed_line(line_id="1", description="Anything")], [], 2.0)
		self.assertEqual([r.method for r in results], [matcher.METHOD_UNMATCHED])

	def test_price_variance_within_tolerance(self):
		lines = [parsed_line(line_id="1", buyer_item_code="A", qty=10, unit_price=101.0)]
		pos = [po_line(item_code="A", qty=10, rate=100.0, remaining_qty=10)]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertAlmostEqual(result.price_variance_pct, 1.0, places=6)
		self.assertTrue(result.within_tolerance)
		all_matched, exceptions = matcher.summarize_match([result], 2.0)
		self.assertTrue(all_matched)
		self.assertEqual(exceptions, [])

	def test_price_variance_outside_tolerance(self):
		lines = [parsed_line(line_id="1", buyer_item_code="A", qty=10, unit_price=110.0)]
		pos = [po_line(item_code="A", qty=10, rate=100.0, remaining_qty=10, po_name="PUR-ORD-1")]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertAlmostEqual(result.price_variance_pct, 10.0, places=6)
		self.assertFalse(result.within_tolerance)
		all_matched, exceptions = matcher.summarize_match([result], 2.0)
		self.assertFalse(all_matched)
		self.assertTrue(any("price" in e for e in exceptions), exceptions)
		self.assertTrue(any("PUR-ORD-1" in e for e in exceptions), exceptions)

	def test_qty_variance(self):
		lines = [parsed_line(line_id="1", buyer_item_code="A", qty=12, unit_price=100.0)]
		pos = [po_line(item_code="A", qty=10, rate=100.0, remaining_qty=10, po_name="PUR-ORD-1")]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertAlmostEqual(result.qty_variance_pct, 20.0, places=6)
		self.assertFalse(result.within_tolerance)
		_all_matched, exceptions = matcher.summarize_match([result], 2.0)
		self.assertTrue(any("quantity" in e for e in exceptions), exceptions)
		self.assertTrue(any("still\nopen" in e or "still open" in e for e in exceptions), exceptions)

	def test_over_billing_against_remaining_qty_is_flagged(self):
		# ordered 10, already billed 8 -> only 2 open; invoicing 10 is inside
		# the qty tolerance vs the order but must still be flagged
		lines = [parsed_line(line_id="1", buyer_item_code="A", qty=10, unit_price=100.0)]
		pos = [po_line(item_code="A", qty=10, rate=100.0, remaining_qty=2, po_name="PUR-ORD-1")]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertTrue(result.within_tolerance)
		all_matched, exceptions = matcher.summarize_match([result], 2.0)
		self.assertFalse(all_matched)
		self.assertTrue(any("exceeds" in e for e in exceptions), exceptions)

	def test_zero_rate_baseline_does_not_divide_by_zero(self):
		lines = [parsed_line(line_id="1", buyer_item_code="A", qty=1, unit_price=5.0)]
		pos = [po_line(item_code="A", qty=1, rate=0.0)]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.price_variance_pct, 100.0)
		self.assertFalse(result.within_tolerance)

	def test_each_po_line_is_claimed_once(self):
		lines = [
			parsed_line(line_id="1", buyer_item_code="A", qty=1, unit_price=1.0),
			parsed_line(line_id="2", buyer_item_code="A", qty=1, unit_price=1.0),
		]
		pos = [po_line(item_code="A", qty=1, rate=1.0, po_detail="r1")]
		first, second = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(first.po_line["po_detail"], "r1")
		self.assertIsNone(second.po_line)

	def test_strong_evidence_runs_before_weak_evidence(self):
		# line 1 would fuzzy-match the only PO line; line 2 names it exactly.
		# The item-code pass must run across all lines first, so line 2 wins.
		lines = [
			parsed_line(line_id="1", description="Steel bolt M8 galvanised"),
			parsed_line(line_id="2", buyer_item_code="BOLT-M8", description="unrelated text"),
		]
		pos = [po_line(item_code="BOLT-M8", description="Steel bolt M8 galvanised", po_detail="r1")]
		first, second = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(first.method, matcher.METHOD_UNMATCHED)
		self.assertEqual(second.method, matcher.METHOD_ITEM_CODE)

	def test_results_are_in_invoice_line_order_and_deterministic(self):
		lines = [
			parsed_line(line_id="1", description="widget blue deluxe"),
			parsed_line(line_id="2", buyer_item_code="B"),
			parsed_line(line_id="3", order_line_ref="3"),
			parsed_line(line_id="4", description="nothing like the others at all"),
		]
		pos = [
			po_line(item_code="A", description="Widget, blue (deluxe)", po_detail="r1"),
			po_line(item_code="B", description="bee", po_detail="r2"),
			po_line(item_code="C", description="cee", po_detail="r3"),
		]
		runs = [matcher.match_lines(lines, pos, 2.0) for _ in range(5)]
		signature = [(r.parsed_line.line_id, r.method, (r.po_line or {}).get("po_detail")) for r in runs[0]]
		self.assertEqual(
			signature,
			[
				("1", matcher.METHOD_DESCRIPTION, "r1"),
				("2", matcher.METHOD_ITEM_CODE, "r2"),
				("3", matcher.METHOD_ORDER_LINE_REF, "r3"),
				("4", matcher.METHOD_UNMATCHED, None),
			],
		)
		for run in runs[1:]:
			self.assertEqual(
				[(r.parsed_line.line_id, r.method, (r.po_line or {}).get("po_detail")) for r in run],
				signature,
			)

	def test_fuzzy_tie_breaks_on_po_position(self):
		lines = [parsed_line(line_id="1", description="identical text")]
		pos = [
			po_line(item_code="A", description="identical text", po_detail="r1"),
			po_line(item_code="B", description="identical text", po_detail="r2"),
		]
		(result,) = matcher.match_lines(lines, pos, 2.0)
		self.assertEqual(result.po_line["po_detail"], "r1")

	def test_summarize_flags_every_unmatched_line(self):
		lines = [parsed_line(line_id="1", item_name="Ghost"), parsed_line(line_id="2", item_name="Phantom")]
		results = matcher.match_lines(lines, [], 2.0)
		all_matched, exceptions = matcher.summarize_match(results, 2.0)
		self.assertFalse(all_matched)
		self.assertEqual(len(exceptions), 2)
		self.assertTrue(all("no matching Purchase Order line" in e for e in exceptions))

	def test_summarize_of_empty_result_set(self):
		all_matched, exceptions = matcher.summarize_match([], 2.0)
		self.assertFalse(all_matched)
		self.assertEqual(exceptions, ["The invoice has no lines to match"])

	def test_normalize(self):
		self.assertEqual(matcher.normalize("  Widget,  BLUE (deluxe)! "), "widget blue deluxe")
		self.assertEqual(matcher.normalize(None), "")


class TestMatcherAgainstRoundTrippedInvoice(unittest.TestCase):
	"""End-to-end on the pure side: builder -> parser -> matcher."""

	def test_round_tripped_lines_match_a_purchase_order(self):
		parsed = parser.parse_ubl_invoice(ubl_builder.build_invoice_xml(make_outbound_fixture()))
		# the outbound builder emits no buyer item codes, so this leans on the
		# fuzzy description pass — exactly the real-world "supplier did not
		# echo our item code" case
		pos = [
			po_line(
				item_code="WIDGET",
				description="Widget, blue <deluxe>",
				qty=2,
				rate=100.0,
				remaining_qty=2,
				po_detail="r1",
			),
			po_line(
				item_code="GRAIN",
				description="Grain, milled",
				qty=5.5,
				rate=10.0,
				remaining_qty=5.5,
				po_detail="r2",
			),
		]
		results = matcher.match_lines(parsed.lines, pos, 2.0)
		self.assertEqual([r.method for r in results], [matcher.METHOD_DESCRIPTION] * 2)
		self.assertEqual([r.po_line["po_detail"] for r in results], ["r1", "r2"])
		all_matched, exceptions = matcher.summarize_match(results, 2.0)
		self.assertTrue(all_matched, exceptions)
		self.assertEqual(exceptions, [])

	def test_a_supplier_price_increase_is_caught_end_to_end(self):
		outbound = make_outbound_fixture()
		outbound.lines[0] = replace(outbound.lines[0], unit_price=115.0, line_net=230.0)
		outbound.net_total = 285.0
		outbound.tax_subtotals[0].taxable_amount = 230.0
		parsed = parser.parse_ubl_invoice(ubl_builder.build_invoice_xml(outbound))
		pos = [
			po_line(
				item_code="WIDGET",
				description="Widget, blue <deluxe>",
				qty=2,
				rate=100.0,
				remaining_qty=2,
				po_name="PUR-ORD-2026-00007",
			)
		]
		results = matcher.match_lines(parsed.lines, pos, 2.0)
		all_matched, exceptions = matcher.summarize_match(results, 2.0)
		self.assertFalse(all_matched)
		self.assertTrue(any("+15.00%" in e for e in exceptions), exceptions)


if __name__ == "__main__":
	unittest.main(verbosity=2)
