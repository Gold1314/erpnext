# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext.accounts.payments_iso20022.builder import SUPPORTED_VARIANTS


class BankPaymentProfile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		batch_booking: DF.Check
		charge_bearer: DF.Literal["SLEV", "SHAR", "DEBT", "CRED"]
		company: DF.Link
		company_bank_account: DF.Link | None
		initiating_party_name: DF.Data | None
		is_default: DF.Check
		notes: DF.SmallText | None
		pain_variant: DF.Literal["pain.001.001.03", "pain.001.001.09"]
		profile_name: DF.Data
		service_level: DF.Literal["SEPA", "NURG", "None"]
	# end: auto-generated types

	def validate(self):
		self.validate_variant()
		self.validate_bank_account()
		self.enforce_single_default()

	def validate_variant(self):
		if self.pain_variant not in SUPPORTED_VARIANTS:
			frappe.throw(
				_("pain.001 variant {0} is not supported. Supported variants: {1}").format(
					self.pain_variant, ", ".join(SUPPORTED_VARIANTS)
				),
				title=_("Unsupported Variant"),
			)

	def validate_bank_account(self):
		"""A profile may only point at a company account of its own company."""
		if not self.company_bank_account:
			return

		account = frappe.db.get_value(
			"Bank Account",
			self.company_bank_account,
			["company", "is_company_account", "iban"],
			as_dict=True,
		)
		if not account:
			return

		if not account.is_company_account:
			frappe.throw(
				_("Bank Account {0} is not marked as a Company Account").format(
					self.company_bank_account
				)
			)
		if account.company and account.company != self.company:
			frappe.throw(
				_("Bank Account {0} belongs to company {1}, not {2}").format(
					self.company_bank_account, account.company, self.company
				)
			)
		if not account.iban:
			frappe.msgprint(
				_(
					"Bank Account {0} has no IBAN. Payment files using this profile will fail "
					"validation until one is entered."
				).format(self.company_bank_account),
				indicator="orange",
				alert=True,
			)

	def enforce_single_default(self):
		"""Keep at most one default profile per company."""
		if not self.is_default:
			return

		others = frappe.get_all(
			"Bank Payment Profile",
			filters={"company": self.company, "is_default": 1, "name": ("!=", self.name)},
			pluck="name",
		)
		for name in others:
			frappe.db.set_value("Bank Payment Profile", name, "is_default", 0)
			frappe.msgprint(
				_("Removed default flag from Bank Payment Profile {0}").format(name),
				alert=True,
			)
