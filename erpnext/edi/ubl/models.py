# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Canonical invoice model for UBL 2.1 / EN 16931 e-invoicing.

Pure domain layer: **no frappe imports**. The Frappe adapter
(``erpnext.edi.ubl.mapper``) maps a Sales Invoice into these dataclasses;
the builder (``erpnext.edi.ubl.builder``) serializes them to UBL XML.

Amounts are plain floats in the *invoice (document) currency* — never in
company/base currency. All cross-checks use a 0.02 tolerance, mirroring the
EN 16931 rounding allowance of one cent per summation side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: rounding tolerance for total cross-checks (EN 16931 calculation rules)
AMOUNT_TOLERANCE = 0.02


@dataclass
class Party:
	"""A trading party (seller or buyer) with its postal address.

	``endpoint_id``/``endpoint_scheme`` carry the electronic address
	(Peppol participant ID, e.g. scheme ``0088`` = GLN, ``9930`` = DE:VAT).
	"""

	name: str
	vat_id: str | None = None
	street: str | None = None
	additional_street: str | None = None
	city: str | None = None
	postal_code: str | None = None
	country_code: str | None = None  # ISO 3166-1 alpha-2, upper-case
	endpoint_id: str | None = None
	endpoint_scheme: str | None = None


@dataclass
class InvoiceLine:
	line_id: str
	description: str
	item_name: str
	qty: float
	unit_code: str  # UN/ECE Recommendation 20 (e.g. C62, KGM, HUR)
	unit_price: float  # net price per unit, invoice currency
	line_net: float  # line extension amount (net), invoice currency
	tax_category_code: str = "S"  # UNCL5305 (S, Z, E, AE, K, G, O, ...)
	tax_rate_pct: float = 0.0


@dataclass
class TaxSubtotal:
	taxable_amount: float
	tax_amount: float
	category_code: str = "S"
	rate_pct: float = 0.0


@dataclass
class CanonicalInvoice:
	invoice_id: str
	issue_date: str  # ISO 8601 date (YYYY-MM-DD)
	due_date: str | None
	currency: str  # ISO 4217, e.g. "EUR"
	seller: Party
	buyer: Party
	lines: list[InvoiceLine] = field(default_factory=list)
	tax_subtotals: list[TaxSubtotal] = field(default_factory=list)
	tax_total: float = 0.0
	net_total: float = 0.0
	grand_total: float = 0.0
	payable_amount: float = 0.0
	invoice_type_code: str = "380"  # UNCL1001: 380 invoice, 381 credit note
	payment_means_code: str | None = None  # UNCL4461, e.g. "30" credit transfer
	payment_terms_note: str | None = None
	buyer_reference: str | None = None
	order_reference: str | None = None
	note: str | None = None
	profile_id: str = ""
	customization_id: str = ""

	def validate(self) -> list[str]:
		"""Return a list of human-readable error strings; empty list = valid."""
		errors: list[str] = []

		if not self.invoice_id:
			errors.append("Invoice ID is missing")
		if not self.issue_date:
			errors.append("Issue date is missing")
		if not self.currency or len(self.currency) != 3 or not self.currency.isalpha():
			errors.append(f"Document currency code {self.currency!r} is not a 3-letter ISO 4217 code")
		if not (self.invoice_type_code and self.invoice_type_code.isdigit()):
			errors.append(f"Invoice type code {self.invoice_type_code!r} is not a numeric UNCL1001 code")

		errors.extend(self._validate_party(self.seller, "Seller", vat_required=True))
		errors.extend(self._validate_party(self.buyer, "Buyer", vat_required=False))

		if not self.lines:
			errors.append("Invoice has no lines")

		for line in self.lines:
			if not line.unit_code:
				errors.append(f"Line {line.line_id}: unit code is missing")
			if not (line.item_name or line.description):
				errors.append(f"Line {line.line_id}: item name and description are both missing")

		sum_lines = sum(line.line_net for line in self.lines)
		if self.lines and abs(sum_lines - self.net_total) > AMOUNT_TOLERANCE:
			errors.append(
				f"Sum of line net amounts ({sum_lines:.2f}) does not match net total ({self.net_total:.2f})"
			)

		if abs((self.net_total + self.tax_total) - self.grand_total) > AMOUNT_TOLERANCE:
			errors.append(
				f"Net total + tax total ({self.net_total + self.tax_total:.2f}) does not "
				f"match grand total ({self.grand_total:.2f})"
			)

		sum_tax = sum(sub.tax_amount for sub in self.tax_subtotals)
		if self.tax_subtotals and abs(sum_tax - self.tax_total) > AMOUNT_TOLERANCE:
			errors.append(
				f"Sum of tax subtotal amounts ({sum_tax:.2f}) does not match tax total ({self.tax_total:.2f})"
			)

		for sub in self.tax_subtotals:
			expected = sub.taxable_amount * sub.rate_pct / 100.0
			if abs(expected - sub.tax_amount) > AMOUNT_TOLERANCE:
				errors.append(
					f"Tax subtotal ({sub.category_code} {sub.rate_pct:g}%): taxable "
					f"{sub.taxable_amount:.2f} x rate gives {expected:.2f}, "
					f"but tax amount is {sub.tax_amount:.2f}"
				)

		return errors

	@staticmethod
	def _validate_party(party: Party | None, role: str, vat_required: bool) -> list[str]:
		errors: list[str] = []
		if party is None:
			return [f"{role} party is missing"]
		if not party.name:
			errors.append(f"{role} name is missing")
		if vat_required and not party.vat_id:
			errors.append(f"{role} VAT identifier (tax ID) is missing")
		cc = party.country_code
		if not cc:
			errors.append(f"{role} country code is missing")
		elif len(cc) != 2 or not cc.isalpha() or not cc.isupper():
			errors.append(f"{role} country code {cc!r} is not a 2-letter upper-case ISO 3166-1 code")
		if party.endpoint_id and not party.endpoint_scheme:
			errors.append(f"{role} endpoint ID is set but its scheme ID is missing")
		return errors
