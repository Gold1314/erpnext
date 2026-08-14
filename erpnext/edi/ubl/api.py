# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for UBL e-invoice generation and transmission.

Lifecycle: Generated -> Queued -> Transmitted / Failed, tracked per attempt
on ``EDI Transmission Log``. v1 transports: "Manual" (user delivers the XML
file themselves; :func:`transmit` marks the log Transmitted) and "API"
(reserved for access-point adapters — see ``erpnext/edi/ubl/DESIGN.md``).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.file_manager import remove_file

from erpnext.edi.ubl.builder import build_invoice_xml
from erpnext.edi.ubl.mapper import sales_invoice_to_canonical
from erpnext.edi.ubl.profiles import get_profile

FALLBACK_PROFILE = "peppol-bis-3"


def _resolve_profile(sales_invoice_doc, profile: str | None):
	"""Resolve (ubl_profile_name, transmission_profile_doc | None).

	Priority: explicit ``profile`` parameter (a UBL profile key *or* an EDI
	Transmission Profile name) -> the company's default EDI Transmission
	Profile -> any EDI Transmission Profile of the company -> bare
	``peppol-bis-3`` with no transmission profile.
	"""
	if profile:
		if frappe.db.exists("EDI Transmission Profile", profile):
			doc = frappe.get_doc("EDI Transmission Profile", profile)
			return doc.ubl_profile, doc
		try:
			get_profile(profile)
		except ValueError as e:
			frappe.throw(str(e), title=_("Unknown UBL Profile"))
		return profile, None

	filters = {"company": sales_invoice_doc.company}
	name = frappe.db.get_value(
		"EDI Transmission Profile", dict(filters, is_default=1)
	) or frappe.db.get_value("EDI Transmission Profile", filters)
	if name:
		doc = frappe.get_doc("EDI Transmission Profile", name)
		return doc.ubl_profile, doc

	return FALLBACK_PROFILE, None


def _build_validated_xml(sales_invoice: str, profile: str | None):
	"""Shared generate/download path: map, validate, build.

	Returns (xml, ubl_profile_name, transmission_profile_doc, warnings).
	Throws with the full error list when validation fails.
	"""
	si = frappe.get_doc("Sales Invoice", sales_invoice)
	frappe.has_permission("Sales Invoice", doc=si, throw=True)

	if si.docstatus != 1:
		frappe.throw(_("Sales Invoice {0} must be submitted before generating an e-invoice").format(si.name))

	ubl_profile, transmission_profile = _resolve_profile(si, profile)

	try:
		canonical, warnings = sales_invoice_to_canonical(
			si.name, ubl_profile, transmission_profile=transmission_profile
		)
	except ValueError as e:
		frappe.throw(str(e), title=_("Unknown UBL Profile"))

	errors = canonical.validate() + get_profile(ubl_profile).check(canonical)
	if errors:
		frappe.throw(
			"<br>".join([_("The e-invoice cannot be generated:"), *errors]),
			title=_("E-Invoice Validation Failed"),
		)

	return build_invoice_xml(canonical), ubl_profile, transmission_profile, warnings


def _attach_xml(si_name: str, file_name: str, content: str):
	"""Attach the XML as a private File, replacing a previous run's file."""
	for old in frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": si_name,
			"file_name": file_name,
		},
		pluck="name",
	):
		remove_file(old, attached_to_doctype="Sales Invoice", attached_to_name=si_name)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": si_name,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.save()
	return file_doc


def _upsert_log(si_name: str, ubl_profile: str, transmission_profile, file_url: str, warnings: list[str]):
	"""Create — or refresh a not-yet-transmitted — EDI Transmission Log."""
	existing = frappe.db.get_value(
		"EDI Transmission Log",
		{
			"sales_invoice": si_name,
			"ubl_profile": ubl_profile,
			"status": ("in", ("Generated", "Failed")),
		},
	)
	log = (
		frappe.get_doc("EDI Transmission Log", existing)
		if existing
		else frappe.new_doc("EDI Transmission Log")
	)
	log.update(
		{
			"sales_invoice": si_name,
			"profile": transmission_profile.name if transmission_profile else None,
			"ubl_profile": ubl_profile,
			"status": "Generated",
			"file_url": file_url,
			"generated_on": now_datetime(),
			"transmitted_on": None,
			"message": None,
			"warnings": "\n".join(warnings) if warnings else None,
		}
	)
	log.save(ignore_permissions=False)
	return log


@frappe.whitelist()
def generate_e_invoice(sales_invoice: str, profile: str | None = None) -> dict:
	"""Generate the UBL XML, attach it to the Sales Invoice and log it.

	Returns ``{"file_url": ..., "warnings": [...], "log": <log name>}``.
	"""
	xml, ubl_profile, transmission_profile, warnings = _build_validated_xml(sales_invoice, profile)

	file_name = f"{sales_invoice}-{ubl_profile}.xml"
	file_doc = _attach_xml(sales_invoice, file_name, xml)
	log = _upsert_log(sales_invoice, ubl_profile, transmission_profile, file_doc.file_url, warnings)

	return {"file_url": file_doc.file_url, "warnings": warnings, "log": log.name}


@frappe.whitelist()
def download_e_invoice(sales_invoice: str, profile: str | None = None):
	"""Stream the UBL XML as a direct download (no attachment, no log)."""
	xml, ubl_profile, _transmission_profile, _warnings = _build_validated_xml(sales_invoice, profile)

	frappe.local.response.filename = f"{sales_invoice}-{ubl_profile}.xml"
	frappe.local.response.filecontent = xml
	frappe.local.response.type = "download"


@frappe.whitelist()
def transmit(log_name: str) -> dict:
	"""Transmit a generated e-invoice according to its profile's transport.

	v1: "Manual" marks the log Transmitted (the user delivers the attached
	XML out of band); "API" is the access-point adapter extension point and
	is not implemented yet.
	"""
	log = frappe.get_doc("EDI Transmission Log", log_name)
	log.check_permission("write")

	if log.status not in ("Generated", "Queued"):
		frappe.throw(
			_("Log {0} is {1}; only Generated or Queued e-invoices can be transmitted").format(
				log.name, _(log.status)
			)
		)

	transport_mode = "Manual"
	if log.profile:
		transport_mode = (
			frappe.db.get_value("EDI Transmission Profile", log.profile, "transport_mode") or "Manual"
		)

	if transport_mode == "Manual":
		log.status = "Transmitted"
		log.transmitted_on = now_datetime()
		log.message = _("Manual delivery: download the attached XML and deliver it to the recipient.")
		log.save()
		return {"status": log.status, "message": log.message}

	# transport_mode == "API"
	frappe.throw(
		_(
			"Transport mode 'API' is not implemented yet. Implement a provider adapter "
			"(see EDIProviderAdapter in erpnext/edi/ubl/DESIGN.md) for your Peppol access "
			"point or national platform, or switch the EDI Transmission Profile {0} to 'Manual'."
		).format(log.profile),
		title=_("Not Implemented"),
	)
