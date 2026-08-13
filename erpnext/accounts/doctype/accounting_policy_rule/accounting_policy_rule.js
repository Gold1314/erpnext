// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Accounting Policy Rule", {
	setup(frm) {
		const account_query = () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				disabled: 0,
			},
		});
		["source_account", "target_account", "manual_debit_account", "manual_credit_account"].forEach(
			(field) => frm.set_query(field, account_query)
		);
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const hints = {
			Reclassify: __(
				"In book {0}, {1}% of the period movement of {2} is moved to {3}.",
				[
					frm.doc.finance_book,
					frm.doc.percentage,
					frm.doc.source_account,
					frm.doc.target_account,
				]
			),
			Exclude: __(
				"In book {0}, {1}% of the period movement of {2} is negated, offset to {3}.",
				[
					frm.doc.finance_book,
					frm.doc.percentage,
					frm.doc.source_account,
					frm.doc.target_account,
				]
			),
			"Manual Amount": __("In book {0}, a fixed entry debits {1} and credits {2}.", [
				frm.doc.finance_book,
				frm.doc.manual_debit_account,
				frm.doc.manual_credit_account,
			]),
		};
		frm.set_intro(hints[frm.doc.rule_type], frm.doc.enabled ? "blue" : "orange");
		if (!frm.doc.enabled) {
			frm.set_intro(__("This rule is disabled and will be skipped by Finance Book Adjustments."), "orange");
		}
	},
});
