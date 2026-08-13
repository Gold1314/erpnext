# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EliminationRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.elimination_rule_account.elimination_rule_account import (
			EliminationRuleAccount,
		)

		account_pairs: DF.Table[EliminationRuleAccount]
		description: DF.SmallText | None
		enabled: DF.Check
		parent_company: DF.Link
		rule_name: DF.Data
		rule_type: DF.Literal["Intercompany Balance", "Investment vs Equity", "Manual Pair"]
	# end: auto-generated types

	def validate(self):
		self.validate_pairs()

	def validate_pairs(self):
		for row in self.account_pairs:
			if row.company_a == row.company_b and row.account_a == row.account_b:
				frappe.throw(
					_("Row {0}: both sides of the pair are the same account ({1} in {2}).").format(
						row.idx, row.account_a, row.company_a
					)
				)

			for side, company_field, account_field in (
				("A", "company_a", "account_a"),
				("B", "company_b", "account_b"),
			):
				company = row.get(company_field)
				account = row.get(account_field)
				if not (company and account):
					continue
				account_company = frappe.get_cached_value("Account", account, "company")
				if account_company and account_company != company:
					frappe.throw(
						_("Row {0}: Account {1} ({2}) does not belong to Company {3} ({4}).").format(
							row.idx, side, account, side, company
						)
					)
