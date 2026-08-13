# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Canonical payment-initiation model for ISO 20022 pain.001 credit transfers.

Pure domain layer: **no frappe imports**. The Frappe adapter
(``erpnext.accounts.payments_iso20022.mapper``) maps a submitted Payment Order
onto these dataclasses; the builder
(``erpnext.accounts.payments_iso20022.builder``) serializes them to a
pain.001.001.03 / pain.001.001.09 XML file that can be uploaded to a bank.

Determinism
-----------
Nothing in this module reads the clock. ``PaymentBatch.creation_date_time`` is
**passed in** by the caller (``api.py`` supplies ``frappe.utils.now_datetime()``)
so that a given batch always serializes to a byte-identical file — which is what
makes the output testable and reproducible for audit.

Amounts are plain floats in the *instructed* currency of each transaction. The
control sum is compared at two decimal places, the precision the file carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: pain.001 caps <MsgId> and <PmtInfId> at 35 characters (Max35Text).
MAX_ID_LENGTH = 35

#: pain.001 caps a single unstructured remittance line at 140 characters.
MAX_REMITTANCE_LENGTH = 140

#: IBAN registry bounds: shortest is Norway (15), longest is 34 by definition.
IBAN_MIN_LENGTH = 15
IBAN_MAX_LENGTH = 34

#: ISO 20022 charge bearer codes (ChargeBearerType1Code).
CHARGE_BEARERS = ("SLEV", "SHAR", "DEBT", "CRED")


def normalize_iban(value: str) -> str:
	"""Strip the spaces banks print for readability and upper-case the result."""
	return "".join(value.split()).upper()


def is_valid_iban(value: str | None) -> bool:
	"""Validate an IBAN structurally and by its ISO 7064 MOD-97-10 checksum.

	The check digits (positions 3-4) are computed over the account identifier
	with the first four characters rotated to the end and every letter replaced
	by its position in the alphabet plus 9 (A=10 ... Z=35). A valid IBAN leaves
	a remainder of exactly 1.

	This is a real checksum test, not a length test: a single mistyped digit in
	an otherwise well-formed IBAN is rejected here rather than by the bank.
	"""
	if not value:
		return False

	iban = normalize_iban(value)

	if not (IBAN_MIN_LENGTH <= len(iban) <= IBAN_MAX_LENGTH):
		return False
	if not iban.isalnum() or not iban.isascii():
		return False
	if not (iban[0:2].isalpha() and iban[2:4].isdigit()):
		return False

	rotated = iban[4:] + iban[:4]
	digits = []
	for char in rotated:
		if char.isdigit():
			digits.append(char)
		else:
			digits.append(str(ord(char) - ord("A") + 10))

	# Fold incrementally: int(...) on a 60+ digit string is fine in Python, but
	# folding keeps the arithmetic small and mirrors the ISO 7064 description.
	remainder = 0
	for chunk_char in "".join(digits):
		remainder = (remainder * 10 + int(chunk_char)) % 97

	return remainder == 1


def is_valid_bic(value: str | None) -> bool:
	"""BIC/SWIFT is 8 (institution) or 11 (institution + branch) characters.

	Layout: 4 alphabetic institution code, 2 alphabetic ISO 3166 country code,
	2 alphanumeric location code, optional 3 alphanumeric branch code.
	"""
	if not value:
		return False

	bic = value.strip().upper()
	if len(bic) not in (8, 11):
		return False
	if not bic.isalnum() or not bic.isascii():
		return False
	if not bic[0:4].isalpha() or not bic[4:6].isalpha():
		return False
	return True


@dataclass
class PaymentParty:
	"""A debtor or creditor: who is paid/paying and into/out of which account."""

	name: str
	iban: str | None = None
	bic: str | None = None
	#: Non-IBAN account identifier, serialized as ``Othr/Id`` when no IBAN
	#: exists (domestic non-SEPA schemes). Never a substitute for a valid IBAN
	#: in a SEPA file — :meth:`PaymentBatch.validate` still flags the gap.
	account_no: str | None = None
	address_lines: list[str] | None = None
	country: str | None = None  # ISO 3166-1 alpha-2, upper-case


@dataclass
class PaymentTransaction:
	"""One credit transfer instruction (one ``CdtTrfTxInf`` block)."""

	end_to_end_id: str
	amount: float
	currency: str  # ISO 4217, e.g. "EUR"
	creditor: PaymentParty
	requested_execution_date: str = ""  # ISO 8601 date (YYYY-MM-DD)
	remittance_info: str | None = None
	purpose_code: str | None = None  # ISO 20022 ExternalPurpose1Code, e.g. SUPP


@dataclass
class PaymentBatch:
	"""A whole pain.001 message: one group header, one payment-information block.

	One ``PmtInf`` block is deliberate — a Payment Order debits exactly one
	company bank account on one execution date, which is precisely the grouping
	key ISO 20022 uses for ``PmtInf``.
	"""

	message_id: str
	creation_date_time: str  # ISO 8601 datetime, PASSED IN — never generated here
	debtor: PaymentParty
	payments: list[PaymentTransaction] = field(default_factory=list)
	batch_booking: bool = False
	payment_method: str = "TRF"
	charge_bearer: str = "SLEV"
	#: ``SEPA``/``NURG``; ``None`` (or the string "None") omits ``PmtTpInf``.
	service_level: str | None = "SEPA"
	#: Initiating party name; falls back to the debtor name when unset.
	initiating_party_name: str | None = None
	#: Defaults to the derived values when left as ``None`` so that a caller can
	#: deliberately supply a *wrong* control sum and have validate() catch it.
	control_sum: float | None = None
	number_of_transactions: int | None = None

	def __post_init__(self) -> None:
		if self.control_sum is None:
			self.control_sum = self.derived_control_sum()
		if self.number_of_transactions is None:
			self.number_of_transactions = len(self.payments)

	def derived_control_sum(self) -> float:
		"""Sum of instructed amounts, rounded to the file's 2-decimal precision."""
		return round(sum(payment.amount for payment in self.payments), 2)

	@property
	def payment_information_id(self) -> str:
		"""``PmtInfId`` — the message id truncated to Max35Text."""
		return self.message_id[:MAX_ID_LENGTH]

	def is_single_currency(self, currency: str) -> bool:
		return bool(self.payments) and all(
			payment.currency.upper() == currency for payment in self.payments
		)

	def execution_date(self) -> str:
		"""The single ``ReqdExctnDt`` of the ``PmtInf`` block.

		A ``PmtInf`` block carries one execution date, so mixed dates would be
		silently collapsed. :meth:`validate` rejects that up front; here we take
		the earliest so a validated batch is unambiguous.
		"""
		dates = [p.requested_execution_date for p in self.payments if p.requested_execution_date]
		return min(dates) if dates else ""

	def validate(self) -> list[str]:
		"""Return a list of human-readable error strings; empty list = valid."""
		errors: list[str] = []

		if not self.message_id:
			errors.append("Message ID is missing")
		elif len(self.message_id) > MAX_ID_LENGTH:
			errors.append(
				f"Message ID {self.message_id!r} is {len(self.message_id)} characters; "
				f"pain.001 allows at most {MAX_ID_LENGTH}"
			)

		if not self.creation_date_time:
			errors.append("Creation date/time is missing")

		if self.payment_method != "TRF":
			errors.append(f"Payment method {self.payment_method!r} is not supported (expected 'TRF')")

		if self.charge_bearer not in CHARGE_BEARERS:
			errors.append(
				f"Charge bearer {self.charge_bearer!r} is not one of {', '.join(CHARGE_BEARERS)}"
			)

		errors.extend(self._validate_party(self.debtor, "Debtor"))

		if not self.payments:
			errors.append("The payment batch has no transactions")

		seen_ids: set[str] = set()
		duplicates: list[str] = []
		for index, payment in enumerate(self.payments, start=1):
			label = f"Transaction {index} ({payment.end_to_end_id or 'no end-to-end ID'})"

			if not payment.end_to_end_id:
				errors.append(f"{label}: End-to-End ID is missing")
			else:
				if len(payment.end_to_end_id) > MAX_ID_LENGTH:
					errors.append(
						f"{label}: End-to-End ID is {len(payment.end_to_end_id)} characters; "
						f"pain.001 allows at most {MAX_ID_LENGTH}"
					)
				if payment.end_to_end_id in seen_ids:
					duplicates.append(payment.end_to_end_id)
				seen_ids.add(payment.end_to_end_id)

			if payment.amount is None or payment.amount <= 0:
				errors.append(f"{label}: amount must be greater than zero (got {payment.amount})")

			currency = payment.currency or ""
			if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
				errors.append(
					f"{label}: currency {payment.currency!r} is not a 3-letter upper-case "
					"ISO 4217 code"
				)

			if not payment.requested_execution_date:
				errors.append(f"{label}: requested execution date is missing")

			errors.extend(self._validate_party(payment.creditor, label))

		for duplicate in sorted(set(duplicates)):
			errors.append(
				f"End-to-End ID {duplicate!r} is used by more than one transaction; "
				"banks reject duplicate end-to-end references within a message"
			)

		execution_dates = {p.requested_execution_date for p in self.payments if p.requested_execution_date}
		if len(execution_dates) > 1:
			errors.append(
				"All transactions must share one requested execution date (the file has a "
				f"single payment-information block); found {', '.join(sorted(execution_dates))}"
			)

		derived_sum = self.derived_control_sum()
		if self.control_sum is None or round(self.control_sum, 2) != derived_sum:
			errors.append(
				f"Control sum {self.control_sum} does not match the sum of instructed "
				f"amounts ({derived_sum:.2f})"
			)

		if self.number_of_transactions != len(self.payments):
			errors.append(
				f"Number of transactions ({self.number_of_transactions}) does not match the "
				f"{len(self.payments)} transaction(s) in the batch"
			)

		return errors

	@staticmethod
	def _validate_party(party: PaymentParty | None, role: str) -> list[str]:
		errors: list[str] = []
		if party is None:
			return [f"{role}: party is missing"]

		if not party.name:
			errors.append(f"{role}: name is missing")

		if not party.iban:
			errors.append(f"{role}: IBAN is missing")
		elif not is_valid_iban(party.iban):
			errors.append(
				f"{role}: IBAN {party.iban!r} is not a valid IBAN "
				"(structure or MOD-97 check digits are wrong)"
			)

		if party.bic and not is_valid_bic(party.bic):
			errors.append(
				f"{role}: BIC {party.bic!r} is not a valid 8 or 11 character BIC/SWIFT code"
			)

		if party.country and (len(party.country) != 2 or not party.country.isalpha()):
			errors.append(f"{role}: country {party.country!r} is not a 2-letter ISO 3166-1 code")

		return errors
