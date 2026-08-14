# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Whitelisted endpoints for ISO 20022 pain.001 bank payment file generation.

Lifecycle: ``Generated`` -> ``Transmitted`` (or ``Failed``), tracked per
attempt on ``Bank Payment File Log``.

v1 transmission is **manual on purpose**: the file is attached to the Payment
Order, the treasurer uploads it to the bank's portal, and :func:`mark_transmitted`
records that acknowledgement. Direct bank connectivity (EBICS, host-to-host
SFTP) is a per-bank integration and belongs behind an adapter — see
``erpnext/accounts/payments_iso20022/DESIGN.md``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.file_manager import remove_file

from erpnext.accounts.payments_iso20022.builder import (
	DEFAULT_VARIANT,
	SUPPORTED_VARIANTS,
	build_pain001,
)
from erpnext.accounts.payments_iso20022.mapper import payment_order_to_batch


def _resolve_profile(payment_order_doc, profile: str | None):
	"""Resolve the ``Bank Payment Profile`` to use, or throw with instructions.

	Priority: explicit ``profile`` parameter -> the company default profile ->
	any profile for this company bank account -> any profile for the company.
	"""
	if profile:
		if not frappe.db.exists("Bank Payment Profile", profile):
			frappe.throw(
				_("Bank Payment Profile {0} does not exist").format(profile),
				title=_("Unknown Payment Profile"),
			)
		return frappe.get_doc("Bank Payment Profile", profile)

	company = payment_order_doc.company
	name = (
		frappe.db.get_value("Bank Payment Profile", {"company": company, "is_default": 1})
		or frappe.db.get_value(
			"Bank Payment Profile",
			{"company": company, "company_bank_account": payment_order_doc.company_bank_account},
		)
		or frappe.db.get_value("Bank Payment Profile", {"company": company})
	)

	if not name:
		frappe.throw(
			_(
				"No Bank Payment Profile is configured for {0}. Create one "
				"(Accounts > Bank Payment Profile), set its Company to {0}, pick the "
				"pain.001 variant your bank accepts ({1}) and tick <b>Is Default</b>."
			).format(company, " or ".join(SUPPORTED_VARIANTS)),
			title=_("Bank Payment Profile Missing"),
		)

	return frappe.get_doc("Bank Payment Profile", name)


def _check_profile(profile_doc, payment_order_doc) -> list[str]:
	"""Profile-level checks that the pure model cannot know about."""
	errors: list[str] = []

	if profile_doc.company != payment_order_doc.company:
		errors.append(
			_("Bank Payment Profile {0} belongs to company {1}, but the Payment Order is for {2}").format(
				profile_doc.name, profile_doc.company, payment_order_doc.company
			)
		)

	if (
		profile_doc.company_bank_account
		and profile_doc.company_bank_account != payment_order_doc.company_bank_account
	):
		errors.append(
			_(
				"Bank Payment Profile {0} is restricted to bank account {1}, but the Payment "
				"Order debits {2}"
			).format(
				profile_doc.name,
				profile_doc.company_bank_account,
				payment_order_doc.company_bank_account,
			)
		)

	if profile_doc.pain_variant not in SUPPORTED_VARIANTS:
		errors.append(
			_("Bank Payment Profile {0} requests unsupported variant {1} (supported: {2})").format(
				profile_doc.name, profile_doc.pain_variant, ", ".join(SUPPORTED_VARIANTS)
			)
		)

	return errors


def _build_validated_file(payment_order: str, profile: str | None, execution_date=None):
	"""Shared generate/download path: resolve profile, map, validate, build.

	Returns ``(xml, payment_order_doc, profile_doc, batch, warnings)`` and
	throws with the full error list when validation fails.
	"""
	doc = frappe.get_doc("Payment Order", payment_order)
	frappe.has_permission("Payment Order", doc=doc, throw=True)

	if doc.docstatus != 1:
		frappe.throw(
			_("Payment Order {0} must be submitted before a bank payment file can be generated").format(
				doc.name
			)
		)

	profile_doc = _resolve_profile(doc, profile)

	batch, warnings = payment_order_to_batch(
		doc.name,
		profile_name=profile_doc.name,
		execution_date=execution_date,
		creation_date_time=now_datetime().strftime("%Y-%m-%dT%H:%M:%S"),
	)

	errors = _check_profile(profile_doc, doc) + batch.validate()
	if errors:
		frappe.throw(
			"<br>".join([_("The bank payment file cannot be generated:"), *errors]),
			title=_("Payment File Validation Failed"),
		)

	variant = profile_doc.pain_variant or DEFAULT_VARIANT
	return build_pain001(batch, variant), doc, profile_doc, batch, warnings


def _file_name(payment_order: str, variant: str) -> str:
	return f"{payment_order}-{variant}.xml"


def _attach_xml(payment_order: str, file_name: str, content: str):
	"""Attach the XML as a private File, replacing a previous run's file."""
	for old in frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Payment Order",
			"attached_to_name": payment_order,
			"file_name": file_name,
		},
		pluck="name",
	):
		remove_file(old, attached_to_doctype="Payment Order", attached_to_name=payment_order)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "Payment Order",
			"attached_to_name": payment_order,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.save()
	return file_doc


def _create_log(payment_order_doc, profile_doc, batch, file_url: str, warnings: list[str]):
	"""Record this generation attempt.

	A new row per generation (rather than an upsert) is deliberate: each file
	carries its own ``MsgId`` and may already sit in a bank portal, so the
	history of what was produced must stay intact for audit.
	"""
	log = frappe.new_doc("Bank Payment File Log")
	log.update(
		{
			"payment_order": payment_order_doc.name,
			"profile": profile_doc.name,
			"pain_variant": profile_doc.pain_variant,
			"status": "Generated",
			"file_url": file_url,
			"number_of_transactions": batch.number_of_transactions,
			"control_sum": batch.control_sum,
			"currency": batch.payments[0].currency if batch.payments else None,
			"generated_on": now_datetime(),
			"warnings": "\n".join(warnings) if warnings else None,
		}
	)
	log.insert(ignore_permissions=False)
	return log


@frappe.whitelist()
def generate_payment_file(payment_order: str, profile: str | None = None, execution_date=None) -> dict:
	"""Generate the pain.001 file, attach it to the Payment Order and log it.

	Returns ``{"file_url": ..., "warnings": [...], "log": <log name>}``.
	"""
	xml, doc, profile_doc, batch, warnings = _build_validated_file(
		payment_order, profile, execution_date
	)

	variant = profile_doc.pain_variant or DEFAULT_VARIANT
	file_doc = _attach_xml(doc.name, _file_name(doc.name, variant), xml)
	log = _create_log(doc, profile_doc, batch, file_doc.file_url, warnings)

	return {"file_url": file_doc.file_url, "warnings": warnings, "log": log.name}


@frappe.whitelist()
def download_payment_file(payment_order: str, profile: str | None = None, execution_date=None):
	"""Stream the pain.001 file as a direct download (no attachment, no log)."""
	xml, doc, profile_doc, _batch, _warnings = _build_validated_file(
		payment_order, profile, execution_date
	)

	variant = profile_doc.pain_variant or DEFAULT_VARIANT
	frappe.local.response.filename = _file_name(doc.name, variant)
	frappe.local.response.filecontent = xml
	frappe.local.response.type = "download"


@frappe.whitelist()
def mark_transmitted(log_name: str, remarks: str | None = None) -> dict:
	"""Record that the generated file was uploaded to the bank.

	v1 transmission is manual: the treasurer uploads the attached XML to the
	bank portal and confirms it here, which stamps ``transmitted_on`` and moves
	the log out of the "not yet paid" bucket in the Payment File Status report.
	"""
	log = frappe.get_doc("Bank Payment File Log", log_name)
	log.check_permission("write")

	if log.status == "Transmitted":
		frappe.throw(
			_("Bank Payment File Log {0} is already marked as Transmitted on {1}").format(
				log.name, log.transmitted_on
			)
		)

	log.status = "Transmitted"
	log.transmitted_on = now_datetime()
	if remarks:
		log.remarks = remarks
	log.save()

	return {"status": log.status, "transmitted_on": str(log.transmitted_on)}
