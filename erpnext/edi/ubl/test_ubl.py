# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure UBL 2.1 e-invoice core.

``models.py``, ``profiles.py`` and ``builder.py`` have no frappe dependency,
but importing them through the ``erpnext`` package would pull in
``erpnext/__init__.py`` (which imports frappe). So when run as a plain file -

	python erpnext/edi/ubl/test_ubl.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the builder's ``from erpnext.edi.ubl.models import ...``
working without a site.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


try:
	from erpnext.edi.ubl import builder, models, profiles
except Exception:
	# stand-alone run: register stub packages so builder.py's absolute import
	# of erpnext.edi.ubl.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.edi", "erpnext.edi.ubl"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.edi.ubl.models", _HERE / "models.py")
	profiles = _load_module("erpnext.edi.ubl.profiles", _HERE / "profiles.py")
	builder = _load_module("erpnext.edi.ubl.builder", _HERE / "builder.py")


NS = {
	"inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
	"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
	"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def make_fixture() -> models.CanonicalInvoice:
	"""Two lines at two tax rates (19% / 7%), EUR, all cross-sums consistent."""
	seller = models.Party(
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
	buyer = models.Party(
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
		models.InvoiceLine(
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
		models.InvoiceLine(
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
		models.TaxSubtotal(taxable_amount=200.0, tax_amount=38.0, category_code="S", rate_pct=19.0),
		models.TaxSubtotal(taxable_amount=55.0, tax_amount=3.85, category_code="S", rate_pct=7.0),
	]
	inv = models.CanonicalInvoice(
		invoice_id="ACC-SINV-2026-00042",
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
		order_reference="PO-7788",
		note="Delivery per agreement & incoterms <EXW>",
	)
	profiles.get_profile("peppol-bis-3").apply(inv)
	return inv


class TestValidation(unittest.TestCase):
	def test_fixture_is_valid(self):
		self.assertEqual(make_fixture().validate(), [])

	def test_totals_mismatch_is_caught(self):
		inv = make_fixture()
		inv.net_total = 999.0
		errors = inv.validate()
		self.assertTrue(any("net total" in e for e in errors), errors)
		self.assertTrue(any("grand total" in e for e in errors), errors)

	def test_tax_subtotal_sum_mismatch_is_caught(self):
		inv = make_fixture()
		inv.tax_subtotals[0].tax_amount = 1.0
		errors = inv.validate()
		self.assertTrue(any("tax subtotal" in e.lower() for e in errors), errors)

	def test_bad_country_code_is_caught(self):
		inv = make_fixture()
		inv.buyer.country_code = "Germany"
		errors = inv.validate()
		self.assertTrue(any("Buyer country code" in e for e in errors), errors)

		inv = make_fixture()
		inv.seller.country_code = "de"  # must be upper-case
		errors = inv.validate()
		self.assertTrue(any("Seller country code" in e for e in errors), errors)

	def test_missing_seller_vat_is_caught(self):
		inv = make_fixture()
		inv.seller.vat_id = None
		errors = inv.validate()
		self.assertTrue(any("Seller VAT" in e for e in errors), errors)

	def test_missing_country_is_caught(self):
		inv = make_fixture()
		inv.seller.country_code = None
		errors = inv.validate()
		self.assertTrue(any("Seller country code is missing" in e for e in errors), errors)


class TestProfiles(unittest.TestCase):
	def test_xrechnung_flags_missing_buyer_reference_but_peppol_passes(self):
		inv = make_fixture()
		inv.buyer_reference = None
		self.assertEqual(profiles.get_profile("peppol-bis-3").check(inv), [])
		errors = profiles.get_profile("xrechnung-3").check(inv)
		self.assertTrue(any("Buyer Reference" in e for e in errors), errors)

	def test_xrechnung_flags_missing_seller_endpoint(self):
		inv = make_fixture()
		inv.seller.endpoint_id = None
		errors = profiles.get_profile("xrechnung-3").check(inv)
		self.assertTrue(any("electronic address" in e for e in errors), errors)

	def test_xrechnung_passes_when_complete(self):
		inv = make_fixture()
		self.assertEqual(profiles.get_profile("xrechnung-3").check(inv), [])

	def test_profile_ids(self):
		p = profiles.get_profile("peppol-bis-3")
		self.assertEqual(
			p.customization_id,
			"urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0",
		)
		self.assertEqual(p.profile_id, "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0")
		self.assertEqual(profiles.get_profile("en16931").customization_id, "urn:cen.eu:en16931:2017")

	def test_unknown_profile_raises(self):
		with self.assertRaises(ValueError):
			profiles.get_profile("fatturapa-99")


class TestBuilder(unittest.TestCase):
	def setUp(self):
		self.inv = make_fixture()
		self.xml = builder.build_invoice_xml(self.inv)
		self.root = ET.fromstring(self.xml)

	def test_root_element_and_namespaces(self):
		self.assertEqual(self.root.tag, f"{{{NS['inv']}}}Invoice")
		self.assertIn(
			'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"', self.xml
		)
		self.assertIn(
			'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"', self.xml
		)
		self.assertIn("<cbc:ID>", self.xml)
		self.assertIn("<cac:AccountingSupplierParty>", self.xml)

	def test_header_element_order(self):
		first_eight = [_local(child.tag) for child in list(self.root)[:8]]
		self.assertEqual(
			first_eight,
			[
				"CustomizationID",
				"ProfileID",
				"ID",
				"IssueDate",
				"DueDate",
				"InvoiceTypeCode",
				"Note",
				"DocumentCurrencyCode",
			],
		)

	def test_header_values(self):
		get = lambda p: self.root.findtext(p, namespaces=NS)  # noqa: E731
		self.assertEqual(get("cbc:ID"), "ACC-SINV-2026-00042")
		self.assertEqual(get("cbc:IssueDate"), "2026-08-13")
		self.assertEqual(get("cbc:DueDate"), "2026-09-12")
		self.assertEqual(get("cbc:InvoiceTypeCode"), "380")
		self.assertEqual(get("cbc:DocumentCurrencyCode"), "EUR")
		self.assertEqual(get("cbc:BuyerReference"), "04011000-1234512345-06")
		self.assertEqual(get("cac:OrderReference/cbc:ID"), "PO-7788")
		self.assertEqual(get("cac:PaymentMeans/cbc:PaymentMeansCode"), "30")
		self.assertEqual(get("cac:PaymentTerms/cbc:Note"), "Net 30 days")

	def test_endpoint_id_scheme_attribute(self):
		endpoint = self.root.find("cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", NS)
		self.assertIsNotNone(endpoint)
		self.assertEqual(endpoint.get("schemeID"), "0088")
		self.assertEqual(endpoint.text, "4012345000009")
		buyer_endpoint = self.root.find("cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", NS)
		self.assertEqual(buyer_endpoint.get("schemeID"), "9957")

	def test_supplier_party_structure(self):
		party = self.root.find("cac:AccountingSupplierParty/cac:Party", NS)
		self.assertEqual(party.findtext("cac:PartyName/cbc:Name", namespaces=NS), "Muster & Söhne <GmbH>")
		address = party.find("cac:PostalAddress", NS)
		self.assertEqual(address.findtext("cbc:StreetName", namespaces=NS), "Hauptstraße 1")
		self.assertEqual(address.findtext("cbc:AdditionalStreetName", namespaces=NS), "Gebäude B")
		self.assertEqual(address.findtext("cbc:CityName", namespaces=NS), "Berlin")
		self.assertEqual(address.findtext("cbc:PostalZone", namespaces=NS), "10115")
		self.assertEqual(address.findtext("cac:Country/cbc:IdentificationCode", namespaces=NS), "DE")
		self.assertEqual(party.findtext("cac:PartyTaxScheme/cbc:CompanyID", namespaces=NS), "DE123456789")
		self.assertEqual(party.findtext("cac:PartyTaxScheme/cac:TaxScheme/cbc:ID", namespaces=NS), "VAT")
		self.assertEqual(
			party.findtext("cac:PartyLegalEntity/cbc:RegistrationName", namespaces=NS),
			"Muster & Söhne <GmbH>",
		)

	def test_tax_total_and_subtotal_math(self):
		tax_total = self.root.find("cac:TaxTotal", NS)
		amount = tax_total.find("cbc:TaxAmount", NS)
		self.assertEqual(amount.text, "41.85")
		self.assertEqual(amount.get("currencyID"), "EUR")

		subtotals = tax_total.findall("cac:TaxSubtotal", NS)
		self.assertEqual(len(subtotals), 2)

		first, second = subtotals
		self.assertEqual(first.findtext("cbc:TaxableAmount", namespaces=NS), "200.00")
		self.assertEqual(first.findtext("cbc:TaxAmount", namespaces=NS), "38.00")
		self.assertEqual(first.findtext("cac:TaxCategory/cbc:ID", namespaces=NS), "S")
		self.assertEqual(first.findtext("cac:TaxCategory/cbc:Percent", namespaces=NS), "19.00")
		self.assertEqual(first.findtext("cac:TaxCategory/cac:TaxScheme/cbc:ID", namespaces=NS), "VAT")
		self.assertEqual(second.findtext("cbc:TaxableAmount", namespaces=NS), "55.00")
		self.assertEqual(second.findtext("cbc:TaxAmount", namespaces=NS), "3.85")
		self.assertEqual(second.findtext("cac:TaxCategory/cbc:Percent", namespaces=NS), "7.00")

		# every amount carries the currencyID attribute
		for el in tax_total.iter():
			if _local(el.tag) in ("TaxAmount", "TaxableAmount"):
				self.assertEqual(el.get("currencyID"), "EUR", ET.tostring(el))

	def test_legal_monetary_total(self):
		totals = self.root.find("cac:LegalMonetaryTotal", NS)
		expected = {
			"LineExtensionAmount": "255.00",
			"TaxExclusiveAmount": "255.00",
			"TaxInclusiveAmount": "296.85",
			"PayableAmount": "296.85",
		}
		children = list(totals)
		self.assertEqual([_local(c.tag) for c in children], list(expected))
		for child in children:
			self.assertEqual(child.text, expected[_local(child.tag)])
			self.assertEqual(child.get("currencyID"), "EUR")

	def test_invoice_lines(self):
		lines = self.root.findall("cac:InvoiceLine", NS)
		self.assertEqual(len(lines), 2)

		l1, l2 = lines
		self.assertEqual(l1.findtext("cbc:ID", namespaces=NS), "1")
		q1 = l1.find("cbc:InvoicedQuantity", NS)
		self.assertEqual(q1.text, "2")
		self.assertEqual(q1.get("unitCode"), "C62")
		lea1 = l1.find("cbc:LineExtensionAmount", NS)
		self.assertEqual(lea1.text, "200.00")
		self.assertEqual(lea1.get("currencyID"), "EUR")
		self.assertEqual(l1.findtext("cac:Item/cbc:Name", namespaces=NS), "Widget")
		self.assertEqual(l1.findtext("cac:Item/cbc:Description", namespaces=NS), "Widget, blue <deluxe>")
		self.assertEqual(l1.findtext("cac:Item/cac:ClassifiedTaxCategory/cbc:ID", namespaces=NS), "S")
		self.assertEqual(
			l1.findtext("cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", namespaces=NS), "19.00"
		)
		self.assertEqual(
			l1.findtext("cac:Item/cac:ClassifiedTaxCategory/cac:TaxScheme/cbc:ID", namespaces=NS),
			"VAT",
		)
		price1 = l1.find("cac:Price/cbc:PriceAmount", NS)
		self.assertEqual(price1.text, "100.00")
		self.assertEqual(price1.get("currencyID"), "EUR")

		q2 = l2.find("cbc:InvoicedQuantity", NS)
		self.assertEqual(q2.text, "5.5")  # 4dp trimmed
		self.assertEqual(q2.get("unitCode"), "KGM")
		self.assertEqual(l2.findtext("cbc:LineExtensionAmount", namespaces=NS), "55.00")

	def test_line_element_order(self):
		line = self.root.find("cac:InvoiceLine", NS)
		self.assertEqual(
			[_local(c.tag) for c in line],
			["ID", "InvoicedQuantity", "LineExtensionAmount", "Item", "Price"],
		)

	def test_deterministic_output(self):
		a = builder.build_invoice_xml(make_fixture())
		b = builder.build_invoice_xml(make_fixture())
		self.assertEqual(a, b)
		self.assertEqual(a, self.xml)
		self.assertNotIn("timestamp", a.lower())

	def test_special_characters_are_escaped(self):
		self.assertIn("Muster &amp; Söhne &lt;GmbH&gt;", self.xml)
		self.assertIn("Widget, blue &lt;deluxe&gt;", self.xml)
		self.assertIn("agreement &amp; incoterms &lt;EXW&gt;", self.xml)
		self.assertNotIn("<GmbH>", self.xml)
		# document parses back cleanly (round-trip)
		self.assertEqual(
			self.root.findtext(
				"cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName",
				namespaces=NS,
			),
			"Muster & Söhne <GmbH>",
		)

	def test_optional_elements_are_omitted(self):
		inv = make_fixture()
		inv.note = None
		inv.due_date = None
		inv.buyer_reference = None
		inv.order_reference = None
		inv.payment_means_code = None
		inv.payment_terms_note = None
		root = ET.fromstring(builder.build_invoice_xml(inv))
		for path in (
			"cbc:Note",
			"cbc:DueDate",
			"cbc:BuyerReference",
			"cac:OrderReference",
			"cac:PaymentMeans",
			"cac:PaymentTerms",
		):
			self.assertIsNone(root.find(path, NS), path)
		# order still correct without optionals
		self.assertEqual(
			[_local(c.tag) for c in list(root)[:5]],
			["CustomizationID", "ProfileID", "ID", "IssueDate", "InvoiceTypeCode"],
		)

	def test_quantity_formatting(self):
		self.assertEqual(builder._quantity(2.0), "2")
		self.assertEqual(builder._quantity(5.5), "5.5")
		self.assertEqual(builder._quantity(1.23456), "1.2346")
		self.assertEqual(builder._quantity(0.0), "0")
		self.assertEqual(builder._quantity(1.2300), "1.23")

	def test_xml_declaration(self):
		self.assertTrue(self.xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))


class TestFixtureIndependence(unittest.TestCase):
	def test_deepcopy_fixture_builds_identically(self):
		inv = make_fixture()
		clone = copy.deepcopy(inv)
		self.assertEqual(builder.build_invoice_xml(inv), builder.build_invoice_xml(clone))


if __name__ == "__main__":
	unittest.main(verbosity=2)
