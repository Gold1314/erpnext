# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""UBL customization profiles (CIUS) supported by the generator.

Pure domain layer: **no frappe imports**. Each profile pins the
``CustomizationID``/``ProfileID`` pair written into the XML and contributes
profile-specific required-field checks on top of
:meth:`~erpnext.edi.ubl.models.CanonicalInvoice.validate`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from erpnext.edi.ubl.models import CanonicalInvoice


def _no_extra_checks(inv: CanonicalInvoice) -> list[str]:
	return []


def _check_xrechnung(inv: CanonicalInvoice) -> list[str]:
	"""XRechnung (German CIUS) hard requirements beyond EN 16931."""
	errors: list[str] = []
	if not inv.buyer_reference:
		errors.append(
			"XRechnung requires a Buyer Reference (BT-10, 'Leitweg-ID') — "
			"set the customer's purchase order / routing reference on the invoice"
		)
	if not (inv.seller and inv.seller.endpoint_id):
		errors.append(
			"XRechnung requires a seller electronic address (BT-34) — "
			"set the seller endpoint ID on the EDI Transmission Profile"
		)
	return errors


@dataclass(frozen=True)
class Profile:
	name: str
	customization_id: str
	profile_id: str
	description: str = ""
	#: extra required-field checks; returns a list of error strings
	check: Callable[[CanonicalInvoice], list[str]] = field(default=_no_extra_checks)

	def apply(self, inv: CanonicalInvoice) -> None:
		"""Stamp this profile's identifiers onto the canonical invoice."""
		inv.customization_id = self.customization_id
		inv.profile_id = self.profile_id


PROFILES: dict[str, Profile] = {
	"peppol-bis-3": Profile(
		name="peppol-bis-3",
		customization_id=("urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"),
		profile_id="urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
		description="Peppol BIS Billing 3.0 (pan-European network profile)",
	),
	"xrechnung-3": Profile(
		name="xrechnung-3",
		customization_id=("urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"),
		profile_id="urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
		description="XRechnung 3.0 (German public-sector CIUS)",
		check=_check_xrechnung,
	),
	"en16931": Profile(
		name="en16931",
		customization_id="urn:cen.eu:en16931:2017",
		profile_id="urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
		description="Bare EN 16931 core invoice model",
	),
}


def get_profile(name: str) -> Profile:
	try:
		return PROFILES[name]
	except KeyError:
		known = ", ".join(sorted(PROFILES))
		raise ValueError(f"Unknown UBL profile {name!r}; known profiles: {known}") from None
