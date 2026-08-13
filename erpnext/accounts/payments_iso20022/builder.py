# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""ISO 20022 pain.001 (CustomerCreditTransferInitiation) XML serializer.

Pure domain layer: **no frappe imports** — only ``xml.etree.ElementTree``.
Takes a validated :class:`~erpnext.accounts.payments_iso20022.models.PaymentBatch`
and emits a bank-uploadable credit-transfer initiation file.

Two schema variants are supported, and the differences between them are real
(a v03 file rejected by a v09-only bank is the whole reason this matters):

=========================  ==============================  ==============================
Concern                    ``pain.001.001.03``             ``pain.001.001.09``
=========================  ==============================  ==============================
Namespace                  ...:xsd:pain.001.001.03         ...:xsd:pain.001.001.09
Execution date             ``<ReqdExctnDt>2026-01-05``     ``<ReqdExctnDt><Dt>2026-01-05``
                           ``</ReqdExctnDt>``              ``</Dt></ReqdExctnDt>``
Agent BIC element          ``FinInstnId/BIC``              ``FinInstnId/BICFI``
=========================  ==============================  ==============================

``ReqdExctnDt`` became a *choice* (``Dt`` | ``DtTm``) in the 2019 message set,
and ``FinancialInstitutionIdentification`` renamed ``BIC`` to ``BICFI`` in the
same revision. Everything else this builder emits is common to both.

Output is deterministic: no timestamps are read here (the creation date/time
travels on the batch), element and attribute order is fixed, and amounts are
always formatted to exactly two decimals.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

from erpnext.accounts.payments_iso20022.models import (
	MAX_REMITTANCE_LENGTH,
	PaymentBatch,
	PaymentParty,
	normalize_iban,
)

NS_TEMPLATE = "urn:iso:std:iso:20022:tech:xsd:{variant}"

VARIANT_V03 = "pain.001.001.03"
VARIANT_V09 = "pain.001.001.09"
SUPPORTED_VARIANTS = (VARIANT_V03, VARIANT_V09)

DEFAULT_VARIANT = VARIANT_V09


def namespace_for(variant: str) -> str:
	"""Target namespace URI of a supported pain.001 variant."""
	if variant not in SUPPORTED_VARIANTS:
		raise ValueError(
			f"Unsupported pain.001 variant {variant!r}; supported: {', '.join(SUPPORTED_VARIANTS)}"
		)
	return NS_TEMPLATE.format(variant=variant)


def _amount(value: float) -> str:
	"""Format a monetary amount with exactly two decimals."""
	return f"{value + 0.0:.2f}"  # +0.0 normalizes -0.0


class _Writer:
	"""Namespace-aware element factory for one pain.001 variant.

	The variant namespace is declared once, as a literal ``xmlns`` attribute on
	the root ``Document`` element, and every element below is written with a
	bare local name so it inherits that default namespace — which is exactly
	the shape every bank's pain.001 sample has.

	The alternative (``ET.register_namespace("", uri)``) cannot express two
	variants at once: registering a prefix drops any previous URI holding it,
	so whichever variant registered last would win and the other would come out
	as ``ns0:``. Declaring the namespace on the element sidesteps that global
	table entirely, which also keeps concurrent builds independent.
	"""

	def __init__(self, variant: str):
		self.variant = variant
		self.ns = namespace_for(variant)
		#: ``BIC`` in the 2009 message set, ``BICFI`` from the 2019 set onwards.
		self.bic_tag = "BIC" if variant == VARIANT_V03 else "BICFI"
		#: v09 wraps the requested execution date in a ``Dt``/``DtTm`` choice.
		self.wraps_execution_date = variant != VARIANT_V03

	def root(self, tag: str) -> Element:
		return Element(tag, {"xmlns": self.ns})

	def sub(self, parent: Element, tag: str) -> Element:
		return SubElement(parent, tag)

	def text(self, parent: Element, tag: str, text: str, **attrs: str) -> Element:
		el = SubElement(parent, tag, dict(attrs))
		el.text = text
		return el

	def agent(self, parent: Element, tag: str, bic: str) -> Element:
		"""``DbtrAgt``/``CdtrAgt`` > ``FinInstnId`` > ``BIC``|``BICFI``."""
		agent = self.sub(parent, tag)
		fin_instn = self.sub(agent, "FinInstnId")
		self.text(fin_instn, self.bic_tag, bic.strip().upper())
		return agent

	def account(self, parent: Element, tag: str, party: PaymentParty) -> Element:
		"""``DbtrAcct``/``CdtrAcct`` > ``Id`` > ``IBAN`` (or ``Othr/Id``).

		The ``Othr/Id`` branch keeps non-IBAN domestic schemes serializable;
		``PaymentBatch.validate()`` still reports the missing IBAN, so this can
		only ever be reached deliberately.
		"""
		account = self.sub(parent, tag)
		identifier = self.sub(account, "Id")
		if party.iban:
			self.text(identifier, "IBAN", normalize_iban(party.iban))
		elif party.account_no:
			other = self.sub(identifier, "Othr")
			self.text(other, "Id", party.account_no.strip())
		return account

	def party(self, parent: Element, tag: str, party: PaymentParty) -> Element:
		"""``Dbtr``/``Cdtr`` > ``Nm`` then the optional ``PstlAdr``.

		``PostalAddress`` puts ``Ctry`` *before* ``AdrLine`` in both variants.
		"""
		element = self.sub(parent, tag)
		self.text(element, "Nm", party.name)

		address_lines = [line for line in (party.address_lines or []) if line]
		if party.country or address_lines:
			address = self.sub(element, "PstlAdr")
			if party.country:
				self.text(address, "Ctry", party.country.strip().upper())
			for line in address_lines[:2]:  # Max2 AdrLine in v03
				self.text(address, "AdrLine", line)
		return element

	def execution_date(self, parent: Element, date: str) -> Element:
		element = self.sub(parent, "ReqdExctnDt")
		if self.wraps_execution_date:
			self.text(element, "Dt", date)
		else:
			element.text = date
		return element


def _build_group_header(writer: _Writer, parent: Element, batch: PaymentBatch) -> None:
	"""``GrpHdr``: MsgId, CreDtTm, NbOfTxs, CtrlSum, InitgPty/Nm (schema order)."""
	header = writer.sub(parent, "GrpHdr")
	writer.text(header, "MsgId", batch.message_id)
	writer.text(header, "CreDtTm", batch.creation_date_time)
	writer.text(header, "NbOfTxs", str(batch.number_of_transactions))
	writer.text(header, "CtrlSum", _amount(batch.control_sum or 0.0))
	initiating_party = writer.sub(header, "InitgPty")
	writer.text(initiating_party, "Nm", batch.initiating_party_name or batch.debtor.name)


def _build_transaction(writer: _Writer, parent: Element, payment) -> None:
	"""One ``CdtTrfTxInf``.

	Schema order: PmtId, (PmtTpInf), Amt, (ChrgBr), CdtrAgt, Cdtr, CdtrAcct,
	Purp, RmtInf — ``Purp`` precedes ``RmtInf`` in both variants.
	"""
	transaction = writer.sub(parent, "CdtTrfTxInf")

	payment_id = writer.sub(transaction, "PmtId")
	writer.text(payment_id, "EndToEndId", payment.end_to_end_id)

	amount = writer.sub(transaction, "Amt")
	writer.text(amount, "InstdAmt", _amount(payment.amount), Ccy=payment.currency.upper())

	if payment.creditor.bic:
		writer.agent(transaction, "CdtrAgt", payment.creditor.bic)

	writer.party(transaction, "Cdtr", payment.creditor)
	writer.account(transaction, "CdtrAcct", payment.creditor)

	if payment.purpose_code:
		purpose = writer.sub(transaction, "Purp")
		writer.text(purpose, "Cd", payment.purpose_code.strip().upper())

	if payment.remittance_info:
		remittance = writer.sub(transaction, "RmtInf")
		writer.text(remittance, "Ustrd", payment.remittance_info[:MAX_REMITTANCE_LENGTH])


def _build_payment_information(writer: _Writer, parent: Element, batch: PaymentBatch) -> None:
	"""The single ``PmtInf`` block: one debtor account, one execution date."""
	info = writer.sub(parent, "PmtInf")

	writer.text(info, "PmtInfId", batch.payment_information_id)
	writer.text(info, "PmtMtd", batch.payment_method)
	writer.text(info, "BtchBookg", "true" if batch.batch_booking else "false")
	writer.text(info, "NbOfTxs", str(batch.number_of_transactions))
	writer.text(info, "CtrlSum", _amount(batch.control_sum or 0.0))

	# SEPA is a euro-only service level: claiming it on a non-EUR file is a
	# guaranteed bank rejection, so it is dropped rather than mis-declared.
	service_level = batch.service_level
	if service_level and service_level != "None":
		if service_level != "SEPA" or batch.is_single_currency("EUR"):
			payment_type = writer.sub(info, "PmtTpInf")
			svc_lvl = writer.sub(payment_type, "SvcLvl")
			writer.text(svc_lvl, "Cd", service_level)

	writer.execution_date(info, batch.execution_date())

	writer.party(info, "Dbtr", batch.debtor)
	writer.account(info, "DbtrAcct", batch.debtor)
	if batch.debtor.bic:
		writer.agent(info, "DbtrAgt", batch.debtor.bic)

	writer.text(info, "ChrgBr", batch.charge_bearer)

	for payment in batch.payments:
		_build_transaction(writer, info, payment)


def build_pain001(batch: PaymentBatch, variant: str = DEFAULT_VARIANT) -> str:
	"""Serialize a payment batch to a pain.001 credit-transfer initiation file.

	The caller is responsible for validating first (``batch.validate()``); this
	function serializes whatever it is given. Raises ``ValueError`` for an
	unsupported variant.
	"""
	writer = _Writer(variant)

	root = writer.root("Document")
	initiation = writer.sub(root, "CstmrCdtTrfInitn")

	_build_group_header(writer, initiation, batch)
	_build_payment_information(writer, initiation, batch)

	body = ET.tostring(root, encoding="unicode")
	return '<?xml version="1.0" encoding="UTF-8"?>\n' + body
