# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Unit tests for the pure ISO 20022 pain.001 core.

``models.py`` and ``builder.py`` have no frappe dependency, but importing them
through the ``erpnext`` package would pull in ``erpnext/__init__.py`` (which
imports frappe). So when run as a plain file -

	python erpnext/accounts/payments_iso20022/test_iso20022.py

- the modules are loaded directly from their file paths under their canonical
names, keeping the builder's
``from erpnext.accounts.payments_iso20022.models import ...`` working without a
site. This mirrors ``erpnext/edi/ubl/test_ubl.py``.
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
	from erpnext.accounts.payments_iso20022 import builder, models
except Exception:
	# stand-alone run: register stub packages so builder.py's absolute import of
	# erpnext.accounts.payments_iso20022.models resolves without importing
	# erpnext/__init__.py (which needs frappe)
	for _pkg in ("erpnext", "erpnext.accounts", "erpnext.accounts.payments_iso20022"):
		if _pkg not in sys.modules:
			_stub = types.ModuleType(_pkg)
			_stub.__path__ = []
			sys.modules[_pkg] = _stub

	models = _load_module("erpnext.accounts.payments_iso20022.models", _HERE / "models.py")
	builder = _load_module("erpnext.accounts.payments_iso20022.builder", _HERE / "builder.py")


V03 = builder.VARIANT_V03
V09 = builder.VARIANT_V09

NS_V03 = builder.namespace_for(V03)
NS_V09 = builder.namespace_for(V09)

EXECUTION_DATE = "2026-09-01"
CREATION_DATE_TIME = "2026-08-13T10:15:00"


def _ns(variant: str) -> dict:
	return {"p": builder.namespace_for(variant)}


def _find(root, path: str, variant: str):
	return root.find(path, _ns(variant))


def _findall(root, path: str, variant: str):
	return root.findall(path, _ns(variant))


def make_batch() -> "models.PaymentBatch":
	"""Three EUR payments from one German debtor account.

	Every IBAN here is a publicly documented specimen and passes MOD-97.
	"""
	debtor = models.PaymentParty(
		name="Muster & Söhne <GmbH>",
		iban="DE89 3704 0044 0532 0130 00",
		bic="COBADEFFXXX",
		country="DE",
		address_lines=["Hauptstraße 1", "10115 Berlin"],
	)

	creditors = [
		models.PaymentParty(name="Widgets & Co", iban="GB82WEST12345698765432", bic="NWBKGB2L"),
		models.PaymentParty(name="Fournisseur Léon", iban="FR1420041010050500013M02606", bic="PSSTFRPPPAR"),
		models.PaymentParty(name="Acme <Supplies> Ltd", iban="NL91ABNA0417164300"),
	]

	payments = [
		models.PaymentTransaction(
			end_to_end_id="PINV-0001",
			amount=1234.5,
			currency="EUR",
			creditor=creditors[0],
			requested_execution_date=EXECUTION_DATE,
			remittance_info="Invoice PINV-0001 & PINV-0002",
			purpose_code="SUPP",
		),
		models.PaymentTransaction(
			end_to_end_id="PINV-0002",
			amount=99.99,
			currency="EUR",
			creditor=creditors[1],
			requested_execution_date=EXECUTION_DATE,
			remittance_info="Facture n° 42",
		),
		models.PaymentTransaction(
			end_to_end_id="PINV-0003",
			amount=0.51,
			currency="EUR",
			creditor=creditors[2],
			requested_execution_date=EXECUTION_DATE,
		),
	]

	return models.PaymentBatch(
		message_id="PMO-0001-1",
		creation_date_time=CREATION_DATE_TIME,
		debtor=debtor,
		payments=payments,
		initiating_party_name="Muster GmbH",
	)


class TestIbanValidator(unittest.TestCase):
	"""ISO 7064 MOD-97-10 check digits, not just a length test."""

	VALID = [
		"GB82WEST12345698765432",  # ECBS specimen (United Kingdom)
		"DE89370400440532013000",  # ECBS specimen (Germany)
		"FR1420041010050500013M02606",  # ECBS specimen (France)
		"NL91ABNA0417164300",  # ECBS specimen (Netherlands)
		"BE68539007547034",  # ECBS specimen (Belgium, shortest common)
		"NO9386011117947",  # Norway, 15 chars = shortest IBAN in the registry
	]

	def test_known_good_ibans(self):
		for iban in self.VALID:
			with self.subTest(iban=iban):
				self.assertTrue(models.is_valid_iban(iban))

	def test_spaces_and_case_are_normalized(self):
		self.assertTrue(models.is_valid_iban("gb82 west 1234 5698 7654 32"))
		self.assertEqual(models.normalize_iban("gb82 west 1234"), "GB82WEST1234")

	def test_mutated_digit_fails_checksum(self):
		# same length and structure, one transposed digit -> MOD-97 must reject
		self.assertFalse(models.is_valid_iban("GB82WEST12345698765433"))
		self.assertFalse(models.is_valid_iban("DE89370400440532013001"))

	def test_wrong_check_digits_fail(self):
		self.assertFalse(models.is_valid_iban("GB83WEST12345698765432"))

	def test_structurally_invalid_ibans(self):
		for bad in [
			"",
			None,
			"GB82",  # too short
			"G182WEST12345698765432",  # digit in the country code
			"GBX2WEST12345698765432",  # letter in the check digits
			"GB82WEST-1234-5698-7654",  # non-alphanumeric
			"GB82WEST123456987654321234567890123456",  # too long
		]:
			with self.subTest(iban=bad):
				self.assertFalse(models.is_valid_iban(bad))

	def test_bic_validation(self):
		for good in ["NWBKGB2L", "COBADEFFXXX", "psstfrpppar"]:
			self.assertTrue(models.is_valid_bic(good), good)
		for bad in ["", None, "NWBKGB2", "NWBKGB2LXX", "1WBKGB2L", "NWBK1B2L"]:
			self.assertFalse(models.is_valid_bic(bad), bad)


class TestBuildPain001(unittest.TestCase):
	def setUp(self):
		self.batch = make_batch()

	def _root(self, variant=V09):
		return ET.fromstring(builder.build_pain001(self.batch, variant))

	def test_root_and_namespace(self):
		for variant, namespace in ((V03, NS_V03), (V09, NS_V09)):
			with self.subTest(variant=variant):
				xml = builder.build_pain001(self.batch, variant)
				self.assertTrue(xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
				self.assertIn(f'xmlns="{namespace}"', xml)
				root = ET.fromstring(xml)
				self.assertEqual(root.tag, f"{{{namespace}}}Document")
				self.assertIsNotNone(_find(root, "p:CstmrCdtTrfInitn", variant))

	def test_group_header_totals_are_strings_in_the_file(self):
		root = self._root()
		header = _find(root, "p:CstmrCdtTrfInitn/p:GrpHdr", V09)
		self.assertEqual(_find(header, "p:MsgId", V09).text, "PMO-0001-1")
		self.assertEqual(_find(header, "p:CreDtTm", V09).text, CREATION_DATE_TIME)
		self.assertEqual(_find(header, "p:NbOfTxs", V09).text, "3")
		self.assertEqual(_find(header, "p:CtrlSum", V09).text, "1335.00")
		self.assertEqual(_find(header, "p:InitgPty/p:Nm", V09).text, "Muster GmbH")

	def test_group_header_element_order(self):
		root = self._root()
		header = _find(root, "p:CstmrCdtTrfInitn/p:GrpHdr", V09)
		tags = [child.tag.rsplit("}", 1)[-1] for child in header]
		self.assertEqual(tags, ["MsgId", "CreDtTm", "NbOfTxs", "CtrlSum", "InitgPty"])

	def test_payment_information_block(self):
		root = self._root()
		blocks = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf", V09)
		self.assertEqual(len(blocks), 1, "a Payment Order maps to exactly one PmtInf block")
		info = blocks[0]

		self.assertEqual(_find(info, "p:PmtInfId", V09).text, "PMO-0001-1")
		self.assertEqual(_find(info, "p:PmtMtd", V09).text, "TRF")
		self.assertEqual(_find(info, "p:BtchBookg", V09).text, "false")
		self.assertEqual(_find(info, "p:NbOfTxs", V09).text, "3")
		self.assertEqual(_find(info, "p:CtrlSum", V09).text, "1335.00")
		self.assertEqual(_find(info, "p:ChrgBr", V09).text, "SLEV")
		self.assertEqual(_find(info, "p:Dbtr/p:Nm", V09).text, "Muster & Söhne <GmbH>")
		self.assertEqual(
			_find(info, "p:DbtrAcct/p:Id/p:IBAN", V09).text,
			"DE89370400440532013000",
			"IBAN spaces are stripped on serialization",
		)
		self.assertEqual(_find(info, "p:Dbtr/p:PstlAdr/p:Ctry", V09).text, "DE")

	def test_batch_booking_true_is_lowercase_xsd_boolean(self):
		self.batch.batch_booking = True
		root = self._root()
		self.assertEqual(
			_find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:BtchBookg", V09).text, "true"
		)

	def test_service_level_sepa_for_all_eur(self):
		root = self._root()
		self.assertEqual(
			_find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:PmtTpInf/p:SvcLvl/p:Cd", V09).text, "SEPA"
		)

	def test_service_level_sepa_omitted_when_not_all_eur(self):
		self.batch.payments[0].currency = "USD"
		root = self._root()
		self.assertIsNone(_find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:PmtTpInf", V09))

	def test_service_level_none_omits_payment_type_information(self):
		self.batch.service_level = "None"
		root = self._root()
		self.assertIsNone(_find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:PmtTpInf", V09))

	def test_transactions(self):
		root = self._root()
		transactions = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)
		self.assertEqual(len(transactions), 3)

		self.assertEqual(
			[_find(t, "p:PmtId/p:EndToEndId", V09).text for t in transactions],
			["PINV-0001", "PINV-0002", "PINV-0003"],
		)

		amounts = [_find(t, "p:Amt/p:InstdAmt", V09) for t in transactions]
		self.assertEqual([a.text for a in amounts], ["1234.50", "99.99", "0.51"])
		self.assertEqual([a.get("Ccy") for a in amounts], ["EUR", "EUR", "EUR"])

		self.assertEqual(
			[_find(t, "p:CdtrAcct/p:Id/p:IBAN", V09).text for t in transactions],
			["GB82WEST12345698765432", "FR1420041010050500013M02606", "NL91ABNA0417164300"],
		)
		self.assertEqual(
			[_find(t, "p:Cdtr/p:Nm", V09).text for t in transactions],
			["Widgets & Co", "Fournisseur Léon", "Acme <Supplies> Ltd"],
		)

	def test_creditor_agent_only_when_bic_present(self):
		root = self._root()
		transactions = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)
		self.assertEqual(_find(transactions[0], "p:CdtrAgt/p:FinInstnId/p:BICFI", V09).text, "NWBKGB2L")
		self.assertIsNone(
			_find(transactions[2], "p:CdtrAgt", V09),
			"the third creditor has no BIC, so no CdtrAgt block is emitted",
		)

	def test_purpose_code_only_when_set_and_precedes_remittance(self):
		root = self._root()
		transactions = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)
		self.assertEqual(_find(transactions[0], "p:Purp/p:Cd", V09).text, "SUPP")
		self.assertIsNone(_find(transactions[1], "p:Purp", V09))

		tags = [child.tag.rsplit("}", 1)[-1] for child in transactions[0]]
		self.assertEqual(tags, ["PmtId", "Amt", "CdtrAgt", "Cdtr", "CdtrAcct", "Purp", "RmtInf"])

	def test_remittance_info_omitted_when_absent(self):
		root = self._root()
		transactions = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)
		self.assertEqual(_find(transactions[0], "p:RmtInf/p:Ustrd", V09).text, "Invoice PINV-0001 & PINV-0002")
		self.assertIsNone(_find(transactions[2], "p:RmtInf", V09))

	def test_remittance_info_truncated_to_140_chars(self):
		long_text = "X" * 200
		self.batch.payments[0].remittance_info = long_text
		root = self._root()
		ustrd = _find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:RmtInf/p:Ustrd", V09)
		self.assertEqual(len(ustrd.text), 140)
		self.assertEqual(ustrd.text, "X" * 140)

	def test_non_iban_account_falls_back_to_othr_id(self):
		self.batch.payments[2].creditor = models.PaymentParty(
			name="Domestic Vendor", iban=None, account_no="000123456789"
		)
		root = self._root()
		transactions = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)
		self.assertIsNone(_find(transactions[2], "p:CdtrAcct/p:Id/p:IBAN", V09))
		self.assertEqual(_find(transactions[2], "p:CdtrAcct/p:Id/p:Othr/p:Id", V09).text, "000123456789")

	def test_amounts_always_carry_two_decimals(self):
		self.batch.payments[0].amount = 1000
		self.batch.payments[1].amount = 0.1
		self.batch.payments[2].amount = 2.005
		# rebuild derived totals after mutating the amounts
		self.batch.control_sum = self.batch.derived_control_sum()
		root = self._root()
		amounts = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:Amt/p:InstdAmt", V09)
		self.assertEqual([a.text for a in amounts], ["1000.00", "0.10", "2.00"])

	def test_unsupported_variant_raises(self):
		with self.assertRaises(ValueError):
			builder.build_pain001(self.batch, "pain.001.001.99")


class TestVariantDifferences(unittest.TestCase):
	"""The two schema differences that decide whether a bank accepts the file."""

	def setUp(self):
		self.batch = make_batch()

	def test_v03_uses_plain_requested_execution_date(self):
		root = ET.fromstring(builder.build_pain001(self.batch, V03))
		element = _find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:ReqdExctnDt", V03)
		self.assertEqual(element.text, EXECUTION_DATE)
		self.assertEqual(len(list(element)), 0, "v03 ReqdExctnDt has no child elements")

	def test_v09_wraps_requested_execution_date_in_dt(self):
		root = ET.fromstring(builder.build_pain001(self.batch, V09))
		element = _find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:ReqdExctnDt", V09)
		self.assertIsNone((element.text or "").strip() or None)
		self.assertEqual(_find(element, "p:Dt", V09).text, EXECUTION_DATE)

	def test_v03_uses_bic_and_v09_uses_bicfi(self):
		v03 = ET.fromstring(builder.build_pain001(self.batch, V03))
		v09 = ET.fromstring(builder.build_pain001(self.batch, V09))

		self.assertEqual(
			_find(v03, "p:CstmrCdtTrfInitn/p:PmtInf/p:DbtrAgt/p:FinInstnId/p:BIC", V03).text,
			"COBADEFFXXX",
		)
		self.assertIsNone(_find(v03, "p:CstmrCdtTrfInitn/p:PmtInf/p:DbtrAgt/p:FinInstnId/p:BICFI", V03))

		self.assertEqual(
			_find(v09, "p:CstmrCdtTrfInitn/p:PmtInf/p:DbtrAgt/p:FinInstnId/p:BICFI", V09).text,
			"COBADEFFXXX",
		)
		self.assertIsNone(_find(v09, "p:CstmrCdtTrfInitn/p:PmtInf/p:DbtrAgt/p:FinInstnId/p:BIC", V09))

	def test_creditor_agent_tag_differs_too(self):
		v03 = ET.fromstring(builder.build_pain001(self.batch, V03))
		v09 = ET.fromstring(builder.build_pain001(self.batch, V09))
		self.assertEqual(
			_find(v03, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:CdtrAgt/p:FinInstnId/p:BIC", V03).text,
			"NWBKGB2L",
		)
		self.assertEqual(
			_find(v09, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf/p:CdtrAgt/p:FinInstnId/p:BICFI", V09).text,
			"NWBKGB2L",
		)

	def test_variants_differ_only_where_expected(self):
		v03 = builder.build_pain001(self.batch, V03)
		v09 = builder.build_pain001(self.batch, V09)
		self.assertNotEqual(v03, v09)
		self.assertIn("<ReqdExctnDt>2026-09-01</ReqdExctnDt>", v03)
		self.assertIn("<ReqdExctnDt><Dt>2026-09-01</Dt></ReqdExctnDt>", v09)


class TestDeterminismAndEscaping(unittest.TestCase):
	def test_two_builds_are_byte_identical(self):
		for variant in (V03, V09):
			with self.subTest(variant=variant):
				first = builder.build_pain001(make_batch(), variant)
				second = builder.build_pain001(make_batch(), variant)
				self.assertEqual(first, second)

	def test_no_ambient_time_leaks_into_the_file(self):
		xml = builder.build_pain001(make_batch(), V09)
		self.assertEqual(xml.count(CREATION_DATE_TIME), 1)

	def test_special_characters_are_escaped(self):
		batch = make_batch()
		batch.payments[0].creditor.name = 'Tom & "Jerry" <Ltd>'
		batch.payments[0].remittance_info = "A & B < C > D"
		xml = builder.build_pain001(batch, V09)

		self.assertIn("Tom &amp; \"Jerry\" &lt;Ltd&gt;", xml)
		self.assertIn("A &amp; B &lt; C &gt; D", xml)
		self.assertNotIn("<Ltd>", xml)

		# and it round-trips back to the original text
		root = ET.fromstring(xml)
		transaction = _findall(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:CdtTrfTxInf", V09)[0]
		self.assertEqual(_find(transaction, "p:Cdtr/p:Nm", V09).text, 'Tom & "Jerry" <Ltd>')
		self.assertEqual(_find(transaction, "p:RmtInf/p:Ustrd", V09).text, "A & B < C > D")

	def test_unicode_survives(self):
		xml = builder.build_pain001(make_batch(), V09)
		root = ET.fromstring(xml)
		self.assertEqual(
			_find(root, "p:CstmrCdtTrfInitn/p:PmtInf/p:Dbtr/p:Nm", V09).text, "Muster & Söhne <GmbH>"
		)


class TestValidation(unittest.TestCase):
	def setUp(self):
		self.batch = make_batch()

	def assertHasError(self, errors, fragment):
		self.assertTrue(
			any(fragment in error for error in errors),
			f"expected an error containing {fragment!r}, got {errors}",
		)

	def test_valid_batch_has_no_errors(self):
		self.assertEqual(self.batch.validate(), [])

	def test_derived_totals(self):
		self.assertEqual(self.batch.number_of_transactions, 3)
		self.assertEqual(self.batch.control_sum, 1335.00)

	def test_bad_iban_checksum_is_caught(self):
		self.batch.payments[1].creditor.iban = "GB82WEST12345698765433"
		errors = self.batch.validate()
		self.assertHasError(errors, "is not a valid IBAN")
		self.assertHasError(errors, "PINV-0002")

	def test_missing_iban_is_caught(self):
		self.batch.payments[2].creditor.iban = None
		self.assertHasError(self.batch.validate(), "IBAN is missing")

	def test_missing_debtor_iban_is_caught(self):
		self.batch.debtor.iban = None
		self.assertHasError(self.batch.validate(), "Debtor: IBAN is missing")

	def test_duplicate_end_to_end_ids_are_caught(self):
		self.batch.payments[2].end_to_end_id = "PINV-0001"
		self.assertHasError(self.batch.validate(), "used by more than one transaction")

	def test_control_sum_mismatch_is_caught(self):
		self.batch.control_sum = 1000.00
		self.assertHasError(self.batch.validate(), "Control sum 1000.0 does not match")

	def test_control_sum_matches_after_recompute(self):
		self.batch.payments.append(
			models.PaymentTransaction(
				end_to_end_id="PINV-0004",
				amount=10.00,
				currency="EUR",
				creditor=copy.deepcopy(self.batch.payments[0].creditor),
				requested_execution_date=EXECUTION_DATE,
			)
		)
		self.assertHasError(self.batch.validate(), "does not match")
		self.batch.control_sum = self.batch.derived_control_sum()
		self.batch.number_of_transactions = len(self.batch.payments)
		self.assertEqual(self.batch.validate(), [])

	def test_zero_and_negative_amounts_are_caught(self):
		for bad_amount in (0, -5.0):
			with self.subTest(amount=bad_amount):
				batch = make_batch()
				batch.payments[0].amount = bad_amount
				batch.control_sum = batch.derived_control_sum()
				self.assertHasError(batch.validate(), "amount must be greater than zero")

	def test_bad_currency_is_caught(self):
		for bad_currency in ("eur", "EU", "EURO", ""):
			with self.subTest(currency=bad_currency):
				batch = make_batch()
				batch.payments[0].currency = bad_currency
				self.assertHasError(batch.validate(), "ISO 4217")

	def test_missing_execution_date_is_caught(self):
		self.batch.payments[0].requested_execution_date = ""
		self.assertHasError(self.batch.validate(), "requested execution date is missing")

	def test_mixed_execution_dates_are_caught(self):
		self.batch.payments[0].requested_execution_date = "2026-09-02"
		self.assertHasError(self.batch.validate(), "must share one requested execution date")

	def test_message_id_length_is_capped_at_35(self):
		self.batch.message_id = "X" * 36
		self.assertHasError(self.batch.validate(), "at most 35")

		self.batch.message_id = "X" * 35
		self.assertEqual(self.batch.validate(), [])

	def test_end_to_end_id_length_is_capped_at_35(self):
		self.batch.payments[0].end_to_end_id = "Y" * 36
		self.assertHasError(self.batch.validate(), "End-to-End ID is 36 characters")

	def test_bad_bic_is_caught(self):
		self.batch.payments[0].creditor.bic = "NWBKGB2"
		self.assertHasError(self.batch.validate(), "is not a valid 8 or 11 character BIC")

	def test_bad_charge_bearer_is_caught(self):
		self.batch.charge_bearer = "FREE"
		self.assertHasError(self.batch.validate(), "Charge bearer")

	def test_empty_batch_is_caught(self):
		batch = models.PaymentBatch(
			message_id="PMO-0002-1",
			creation_date_time=CREATION_DATE_TIME,
			debtor=copy.deepcopy(self.batch.debtor),
			payments=[],
		)
		self.assertHasError(batch.validate(), "no transactions")

	def test_missing_message_id_is_caught(self):
		self.batch.message_id = ""
		self.assertHasError(self.batch.validate(), "Message ID is missing")


if __name__ == "__main__":
	unittest.main(verbosity=2)
