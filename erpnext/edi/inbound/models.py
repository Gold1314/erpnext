# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Canonical model for an **inbound** supplier e-invoice (UBL 2.1 / EN 16931).

Pure domain layer: **no frappe imports**. This is the mirror image of
``erpnext.edi.ubl.models``: where the outbound side maps a Sales Invoice
*into* :class:`~erpnext.edi.ubl.models.CanonicalInvoice` and serializes it,
the inbound side parses a supplier's XML *into* :class:`ParsedInvoice` and
maps that onto a draft Purchase Invoice.

The two models are deliberately **not** the same class:

* the outbound model is what we promise to emit — every field is ours and
  validated before it leaves the building;
* the inbound model is what a third party sent us — it carries the extra
  cross-reference fields we need for matching (``buyer_item_code``,
  ``seller_item_code``, ``order_line_ref``) and nothing may be assumed
  present. Optional elements parse to ``None``, never to a guess.

Amounts are plain floats in the *invoice (document) currency* — the currency
named by ``DocumentCurrencyCode``. Cross-checks use the same 0.02 tolerance
as the outbound side (EN 16931 allows one cent of rounding per summation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: rounding tolerance for total cross-checks (EN 16931 calculation rules)
AMOUNT_TOLERANCE = 0.02


@dataclass
class ParsedParty:
	"""A trading party read off the inbound document (seller or buyer).

	``vat_id`` comes from ``cac:PartyTaxScheme/cbc:CompanyID`` (BT-31/BT-48)
	and is the primary key we resolve a Supplier by. ``endpoint_id`` is the
	electronic address (BT-34/BT-49, e.g. a Peppol participant ID).
	"""

	name: str = ""
	vat_id: str | None = None
	endpoint_id: str | None = None
	country: str | None = None  # ISO 3166-1 alpha-2 as sent
	street: str | None = None
	city: str | None = None
	postal_code: str | None = None


@dataclass
class ParsedLine:
	"""One ``cac:InvoiceLine``.

	``buyer_item_code`` (BT-156, ``BuyersItemIdentification``) is *our* item
	code as the supplier recorded it — the highest-confidence matching key.
	``seller_item_code`` (BT-155) is the supplier's own part number, which we
	resolve through the ``Item Supplier`` child table. ``order_line_ref``
	(BT-132) points at the ordered line of the referenced Purchase Order.
	"""

	line_id: str
	description: str = ""
	item_name: str = ""
	qty: float = 0.0
	unit_code: str = ""  # UN/ECE Recommendation 20 (C62, KGM, HUR, ...)
	unit_price: float = 0.0  # net price per unit, invoice currency
	line_net: float = 0.0  # cbc:LineExtensionAmount, invoice currency
	tax_rate_pct: float = 0.0
	tax_category: str = ""  # UNCL5305 (S, Z, E, AE, K, G, O, ...)
	buyer_item_code: str | None = None
	seller_item_code: str | None = None
	order_line_ref: str | None = None


@dataclass
class ParsedTax:
	"""One ``cac:TaxSubtotal`` — the VAT breakdown row for a rate/category."""

	taxable_amount: float = 0.0
	tax_amount: float = 0.0
	category_code: str = ""
	rate_pct: float = 0.0


@dataclass
class ParsedInvoice:
	"""A supplier invoice as received, before any ERPNext resolution."""

	invoice_id: str
	issue_date: str | None  # ISO 8601 date (YYYY-MM-DD) as sent
	due_date: str | None
	currency: str
	invoice_type_code: str
	seller: ParsedParty
	buyer: ParsedParty
	lines: list[ParsedLine] = field(default_factory=list)
	taxes: list[ParsedTax] = field(default_factory=list)
	tax_total: float = 0.0
	net_total: float = 0.0
	grand_total: float = 0.0
	payable_amount: float = 0.0
	order_reference: str | None = None
	buyer_reference: str | None = None
	note: str | None = None
	#: raw CustomizationID (falling back to ProfileID) — recorded verbatim so
	#: the operator can see which CIUS the sender claimed, without us having
	#: to recognize it. Unknown profiles are parsed, not rejected.
	raw_profile: str | None = None

	def validate(self) -> list[str]:
		"""Return a list of human-readable error strings; empty list = valid.

		These are *ingestion* errors — things that make the document unusable
		as the basis of a Purchase Invoice. Anything that is merely unresolved
		on our side (unknown supplier, unmapped item) is a warning produced by
		the mapper, not an error here.
		"""
		errors: list[str] = []

		if not self.invoice_id:
			errors.append("Invoice ID (BT-1) is missing")
		if not self.issue_date:
			errors.append("Issue date (BT-2) is missing")
		if not self.currency or len(self.currency) != 3 or not self.currency.isalpha():
			errors.append(f"Document currency code {self.currency!r} is not a 3-letter ISO 4217 code")

		if self.seller is None or not self.seller.name:
			errors.append("Seller name (BT-27) is missing")
		if self.seller is not None and not self.seller.vat_id:
			errors.append(
				"Seller VAT identifier (BT-31) is missing; the supplier cannot be resolved by tax ID"
			)

		if not self.lines:
			errors.append("Invoice has no lines")

		for line in self.lines:
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

		sum_tax = sum(tax.tax_amount for tax in self.taxes)
		if self.taxes and abs(sum_tax - self.tax_total) > AMOUNT_TOLERANCE:
			errors.append(
				f"Sum of tax subtotal amounts ({sum_tax:.2f}) does not match tax total ({self.tax_total:.2f})"
			)

		return errors
