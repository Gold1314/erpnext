# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Frappe adapter: Payment Order -> :class:`PaymentBatch`.

This is the only ISO 20022 module (besides ``api.py`` and the doctype
controllers) that imports frappe. It reads a submitted Payment Order and its
``references`` child rows and maps them onto the pure payment model.

Field provenance
----------------
Every fieldname below was read off the shipped JSON, not assumed:

============================  ===========================================
Source                        Fields used
============================  ===========================================
Payment Order                 ``company``, ``payment_order_type``,
                              ``company_bank_account``, ``company_bank``,
                              ``posting_date``, ``references``
Payment Order Reference       ``reference_doctype``, ``reference_name``,
                              ``amount``, ``supplier``, ``payment_request``,
                              ``bank_account``
Bank Account                  ``account_name``, ``iban``, ``bank_account_no``,
                              ``bank``, ``party_type``, ``party``,
                              ``is_company_account``, ``disabled``
Bank                          ``swift_number``
Supplier                      ``supplier_name``, ``default_bank_account``
============================  ===========================================

Currency decision
-----------------
A Payment Order carries no currency of its own, and the child row's ``amount``
is stored in the currency of the document it came from:

* ``payment_order_type == "Payment Request"`` — the row amount is the Payment
  Request's ``grand_total``, so the currency is that Payment Request's
  ``currency`` field.
* ``payment_order_type == "Payment Entry"`` — the row amount is the Payment
  Entry's ``paid_amount``, which is denominated in ``paid_from_account_currency``
  (the *company* bank account's currency, since this is an outgoing payment).

When neither can be resolved the company's ``default_currency`` is used and a
warning is recorded. The company default is deliberately the *last* resort: a
mis-stated currency in a payment file moves real money at the wrong amount.

Determinism
-----------
No clock is read here. ``message_id`` is either passed in or derived from the
Payment Order name, and ``creation_date_time`` is supplied by the caller
(``api.py`` passes ``frappe.utils.now_datetime()``), so the mapper itself is a
pure function of database state.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import getdate

from erpnext.accounts.payments_iso20022.models import (
	MAX_ID_LENGTH,
	MAX_REMITTANCE_LENGTH,
	PaymentBatch,
	PaymentParty,
	PaymentTransaction,
	normalize_iban,
)

#: pain.001 identifiers are restricted to a conservative printable subset; SEPA
#: implementation guidelines allow ``a-z A-Z 0-9 / - ? : ( ) . , ' +`` and space.
#: Anything else is replaced with a hyphen so the bank's parser never chokes.
_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")

#: ISO 20022 ExternalPurpose1Code for a supplier payment.
DEFAULT_PURPOSE_CODE = "SUPP"


def sanitize_id(value: str, max_length: int = MAX_ID_LENGTH) -> str:
	"""Make a document name safe and short enough for an ISO 20022 identifier."""
	cleaned = _ID_DISALLOWED.sub("-", (value or "").strip())
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	return cleaned[:max_length]


def _uniquify(candidate: str, taken: set[str], max_length: int = MAX_ID_LENGTH) -> str:
	"""Return ``candidate``, or ``candidate`` with a numeric suffix if taken.

	The suffix eats into the identifier from the right so the result never
	exceeds ``max_length`` — two rows referencing the same document (a partial
	payment split across a Payment Order) must still get distinct end-to-end
	IDs, because banks reject duplicates within one message.
	"""
	if candidate not in taken:
		return candidate

	counter = 2
	while True:
		suffix = f"-{counter}"
		trimmed = candidate[: max_length - len(suffix)].rstrip("-")
		attempt = f"{trimmed}{suffix}"
		if attempt not in taken:
			return attempt
		counter += 1


def _bank_bic(bank: str | None) -> str | None:
	"""``Bank.swift_number`` is ERPNext's BIC field."""
	if not bank:
		return None
	return frappe.db.get_value("Bank", bank, "swift_number") or None


def _bank_account_to_party(
	bank_account_name: str, fallback_name: str | None, warnings: list[str], context: str
) -> PaymentParty:
	"""Map a Bank Account doc onto a :class:`PaymentParty`."""
	account = frappe.db.get_value(
		"Bank Account",
		bank_account_name,
		["account_name", "iban", "bank_account_no", "bank", "party", "disabled"],
		as_dict=True,
	)
	if not account:
		warnings.append(_("{0}: Bank Account {1} no longer exists").format(context, bank_account_name))
		return PaymentParty(name=fallback_name or bank_account_name)

	if account.disabled:
		warnings.append(
			_("{0}: Bank Account {1} is disabled but is still referenced").format(
				context, bank_account_name
			)
		)

	if not account.iban:
		warnings.append(
			_(
				"{0}: Bank Account {1} has no IBAN — the payment file cannot be generated "
				"until one is entered"
			).format(context, bank_account_name)
		)

	bic = _bank_bic(account.bank)
	if not bic:
		warnings.append(
			_("{0}: Bank {1} has no SWIFT number, so no agent BIC is written for this party").format(
				context, account.bank or _("(not set)")
			)
		)

	# The account holder's legal name beats the bank account's label: the
	# creditor name in the file must match the name on the bank account.
	name = account.party or account.account_name or fallback_name or bank_account_name

	return PaymentParty(
		name=name,
		iban=normalize_iban(account.iban) if account.iban else None,
		bic=bic,
		account_no=account.bank_account_no,
	)


def _debtor_party(payment_order, warnings: list[str]) -> PaymentParty:
	"""The company bank account being debited (``company_bank_account``)."""
	if not payment_order.company_bank_account:
		warnings.append(_("Payment Order {0} has no Company Bank Account").format(payment_order.name))
		return PaymentParty(name=payment_order.company)

	party = _bank_account_to_party(
		payment_order.company_bank_account,
		payment_order.company,
		warnings,
		_("Company bank account"),
	)
	# For the debtor we want the company's legal name, not the party field
	# (a company account has no party) nor the account label.
	party.name = payment_order.company
	return party


def _creditor_party(row, warnings: list[str]) -> PaymentParty:
	"""The supplier's bank account for one ``Payment Order Reference`` row.

	``bank_account`` on the row is mandatory in the doctype and is set from the
	Payment Request's ``bank_account`` / the Payment Entry's
	``party_bank_account``. When it is missing anyway (imported or patched
	data), fall back to the Supplier's ``default_bank_account``, and finally to
	a bare supplier name — which leaves the batch invalid on purpose, so the
	user gets a precise error instead of a file the bank silently rejects.
	"""
	supplier_name = None
	if row.supplier:
		supplier_name = frappe.db.get_value("Supplier", row.supplier, "supplier_name") or row.supplier

	context = _("Row {0} ({1})").format(row.idx, row.reference_name)

	bank_account = row.bank_account
	if not bank_account and row.supplier:
		bank_account = frappe.db.get_value("Supplier", row.supplier, "default_bank_account")
		if bank_account:
			warnings.append(
				_("{0}: no bank account on the row, using the supplier's default account {1}").format(
					context, bank_account
				)
			)

	if not bank_account:
		warnings.append(
			_(
				"{0}: supplier {1} has no bank account — no IBAN can be resolved and the "
				"payment file cannot be generated for this row"
			).format(context, supplier_name or _("(unknown)"))
		)
		return PaymentParty(name=supplier_name or _("Unknown Supplier"))

	party = _bank_account_to_party(bank_account, supplier_name, warnings, context)
	if supplier_name and not party.name:
		party.name = supplier_name
	return party


def _row_currency(payment_order, row, company_currency: str, warnings: list[str]) -> str:
	"""Resolve the currency the row's ``amount`` is denominated in."""
	context = _("Row {0} ({1})").format(row.idx, row.reference_name)

	if payment_order.payment_order_type == "Payment Request" and row.payment_request:
		currency = frappe.db.get_value("Payment Request", row.payment_request, "currency")
		if currency:
			return currency.upper()

	if payment_order.payment_order_type == "Payment Entry" and row.reference_doctype == "Payment Entry":
		currency = frappe.db.get_value("Payment Entry", row.reference_name, "paid_from_account_currency")
		if currency:
			return currency.upper()

	warnings.append(
		_("{0}: could not resolve the payment currency, falling back to the company currency {1}").format(
			context, company_currency
		)
	)
	return (company_currency or "").upper()


def _remittance_info(payment_order, row, warnings: list[str]) -> str | None:
	"""Build the unstructured remittance line the supplier will see.

	The goal is that the supplier can reconcile the credit on their statement,
	so the *supplier's own* invoice numbers are preferred over ERPNext names:

	* Payment Request rows — ``reference_doctype``/``reference_name`` already
	  point at the invoice being paid; if it is a Purchase Invoice its
	  ``bill_no`` (the supplier's invoice number) is used when present.
	* Payment Entry rows — the Payment Entry's own ``references`` child rows
	  carry ``bill_no`` per allocated invoice; those are joined.

	Anything longer than 140 characters is truncated by the builder.
	"""
	parts: list[str] = []

	if payment_order.payment_order_type == "Payment Entry" and row.reference_doctype == "Payment Entry":
		allocations = frappe.get_all(
			"Payment Entry Reference",
			filters={"parent": row.reference_name, "parenttype": "Payment Entry"},
			fields=["reference_doctype", "reference_name", "bill_no"],
			order_by="idx asc",
		)
		for allocation in allocations:
			parts.append(allocation.bill_no or allocation.reference_name)
	elif row.reference_doctype and row.reference_name:
		bill_no = None
		if row.reference_doctype == "Purchase Invoice":
			bill_no = frappe.db.get_value("Purchase Invoice", row.reference_name, "bill_no")
		parts.append(bill_no or row.reference_name)

	parts = [part for part in parts if part]
	if not parts:
		warnings.append(
			_("Row {0} ({1}): no invoice reference could be derived for the remittance line").format(
				row.idx, row.reference_name
			)
		)
		return None

	text = ", ".join(dict.fromkeys(parts))
	if len(text) > MAX_REMITTANCE_LENGTH:
		warnings.append(
			_("Row {0} ({1}): remittance information is truncated to {2} characters").format(
				row.idx, row.reference_name, MAX_REMITTANCE_LENGTH
			)
		)
	return text


def payment_order_to_batch(
	payment_order_name: str,
	profile_name: str | None = None,
	execution_date=None,
	message_id: str | None = None,
	creation_date_time: str | None = None,
) -> tuple[PaymentBatch, list[str]]:
	"""Map a Payment Order onto a :class:`PaymentBatch`.

	:param payment_order_name: name of a **submitted** Payment Order.
	:param profile_name: a ``Bank Payment Profile`` supplying the service level,
		charge bearer, batch-booking flag and initiating party name. Optional —
		ISO 20022 defaults are used when omitted.
	:param execution_date: requested execution date for every transaction;
		defaults to the Payment Order's ``posting_date``.
	:param message_id: ``MsgId``; defaults to the Payment Order name plus a
		generation counter (the number of existing logs + 1) so regenerating a
		file yields a *new* message id — banks deduplicate on ``MsgId``.
	:param creation_date_time: ``CreDtTm``; **must** be supplied by the caller
		for a real file. Left ``None`` only in tests, where an empty value makes
		``validate()`` complain rather than silently stamping the clock.

	Returns ``(batch, warnings)``. The batch is *not* validated here — the
	caller runs ``batch.validate()`` so it can present errors and warnings
	together.
	"""
	payment_order = frappe.get_doc("Payment Order", payment_order_name)
	warnings: list[str] = []

	profile = frappe.get_doc("Bank Payment Profile", profile_name) if profile_name else None

	company_currency = frappe.get_cached_value("Company", payment_order.company, "default_currency")

	requested_date = getdate(execution_date) if execution_date else getdate(payment_order.posting_date)
	if not execution_date:
		warnings.append(
			_("No execution date given; using the Payment Order posting date {0}").format(requested_date)
		)
	execution_date_str = str(requested_date)

	debtor = _debtor_party(payment_order, warnings)

	payments: list[PaymentTransaction] = []
	used_ids: set[str] = set()

	for row in payment_order.references:
		end_to_end_id = _uniquify(sanitize_id(row.reference_name), used_ids)
		if end_to_end_id != sanitize_id(row.reference_name):
			warnings.append(
				_("Row {0}: End-to-End ID made unique as {1} (duplicate reference)").format(
					row.idx, end_to_end_id
				)
			)
		used_ids.add(end_to_end_id)

		payments.append(
			PaymentTransaction(
				end_to_end_id=end_to_end_id,
				amount=float(row.amount or 0),
				currency=_row_currency(payment_order, row, company_currency, warnings),
				creditor=_creditor_party(row, warnings),
				requested_execution_date=execution_date_str,
				remittance_info=_remittance_info(payment_order, row, warnings),
				purpose_code=DEFAULT_PURPOSE_CODE,
			)
		)

	batch = PaymentBatch(
		message_id=sanitize_id(message_id or _default_message_id(payment_order.name)),
		creation_date_time=creation_date_time or "",
		debtor=debtor,
		payments=payments,
		batch_booking=bool(profile.batch_booking) if profile else False,
		charge_bearer=(profile.charge_bearer if profile else None) or "SLEV",
		service_level=(profile.service_level if profile else None) or "SEPA",
		initiating_party_name=(profile.initiating_party_name if profile else None)
		or payment_order.company,
	)

	return batch, warnings


def _default_message_id(payment_order_name: str) -> str:
	"""``<Payment Order>-<generation counter>``, never a timestamp.

	The counter is the number of Bank Payment File Logs already recorded for
	this Payment Order plus one, so a regenerated file carries a fresh
	``MsgId`` (banks reject a repeated one as a duplicate submission) while the
	value stays reproducible for a given database state.
	"""
	attempt = frappe.db.count("Bank Payment File Log", {"payment_order": payment_order_name}) + 1
	suffix = f"-{attempt}"
	return f"{payment_order_name[: MAX_ID_LENGTH - len(suffix)]}{suffix}"
