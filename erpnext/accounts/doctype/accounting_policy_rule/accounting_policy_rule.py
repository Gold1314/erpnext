# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext.accounts.multibook.models import EXCLUDE, MANUAL_AMOUNT, RECLASSIFY


class AccountingPolicyRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		description: DF.SmallText | None
		enabled: DF.Check
		finance_book: DF.Link
		manual_amount: DF.Currency
		manual_credit_account: DF.Link | None
		manual_debit_account: DF.Link | None
		percentage: DF.Percent
		rule_name: DF.Data
		rule_type: DF.Literal["Reclassify", "Exclude", "Manual Amount"]
		source_account: DF.Link | None
		target_account: DF.Link | None
	# end: auto-generated types

	def validate(self):
		self.clear_unused_fields()
		self.validate_required_fields()
		self.validate_percentage()
		self.validate_accounts()

	def clear_unused_fields(self):
		"""Stray values of the other rule type must not survive a type switch."""
		if self.rule_type == MANUAL_AMOUNT:
			self.source_account = None
			self.target_account = None
			self.percentage = 0
		else:
			self.manual_amount = 0
			self.manual_debit_account = None
			self.manual_credit_account = None

	def validate_required_fields(self):
		if self.rule_type in (RECLASSIFY, EXCLUDE):
			if not self.source_account or not self.target_account:
				frappe.throw(
					_("{0} rules need both a Source Account and a Target Account.").format(_(self.rule_type))
				)
			if self.source_account == self.target_account:
				frappe.throw(_("Source Account and Target Account must differ."))
		elif self.rule_type == MANUAL_AMOUNT:
			if not self.manual_debit_account or not self.manual_credit_account:
				frappe.throw(
					_("Manual Amount rules need both a Manual Debit Account and a Manual Credit Account.")
				)
			if self.manual_debit_account == self.manual_credit_account:
				frappe.throw(_("Manual Debit Account and Manual Credit Account must differ."))
			if flt(self.manual_amount) <= 0:
				frappe.throw(_("Manual Amount must be greater than zero."))
		else:
			frappe.throw(_("Unknown Rule Type {0}.").format(frappe.bold(self.rule_type)))

	def validate_percentage(self):
		if self.rule_type not in (RECLASSIFY, EXCLUDE):
			return
		if not flt(self.percentage):
			self.percentage = 100
		if not (0 < flt(self.percentage) <= 100):
			frappe.throw(_("Percentage must be greater than 0 and at most 100."))

	def validate_accounts(self):
		account_fields = (
			("source_account", _("Source Account")),
			("target_account", _("Target Account")),
			("manual_debit_account", _("Manual Debit Account")),
			("manual_credit_account", _("Manual Credit Account")),
		)
		for fieldname, label in account_fields:
			account = self.get(fieldname)
			if not account:
				continue
			company, is_group = frappe.db.get_value("Account", account, ["company", "is_group"])
			if company != self.company:
				frappe.throw(
					_("{0} {1} does not belong to company {2}.").format(
						label, frappe.bold(account), frappe.bold(self.company)
					)
				)
			if is_group:
				frappe.throw(_("{0} {1} is a group account; pick a ledger account.").format(label, frappe.bold(account)))
