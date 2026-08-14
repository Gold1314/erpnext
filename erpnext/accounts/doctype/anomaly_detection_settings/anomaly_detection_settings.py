# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class AnomalyDetectionSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		backdate_days_threshold: DF.Int
		benford_min_n: DF.Int
		duplicate_days_window: DF.Int
		enable_account_outliers: DF.Check
		enable_benford: DF.Check
		enable_duplicate_invoices: DF.Check
		enable_rare_combinations: DF.Check
		enable_suspicious_postings: DF.Check
		enabled: DF.Check
		rarity_pct: DF.Float
		round_amount_threshold: DF.Float
		z_threshold: DF.Float
	# end: auto-generated types

	pass
