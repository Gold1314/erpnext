// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bank Payment Profile", {
	setup(frm) {
		frm.set_query("company_bank_account", function () {
			return {
				filters: {
					is_company_account: 1,
					company: frm.doc.company,
				},
			};
		});
	},

	refresh(frm) {
		frm.set_intro(
			frm.doc.pain_variant === "pain.001.001.03"
				? __(
						"pain.001.001.03 is the legacy SEPA version. Confirm with your bank before using it — newer portals often only accept pain.001.001.09."
				  )
				: "",
			"blue"
		);
	},

	company(frm) {
		if (frm.doc.company_bank_account) {
			frm.set_value("company_bank_account", null);
		}
	},

	service_level(frm) {
		if (frm.doc.service_level === "SEPA" && frm.doc.charge_bearer !== "SLEV") {
			frappe.msgprint(
				__("SEPA credit transfers require the charge bearer SLEV."),
				__("Charge Bearer")
			);
		}
	},
});
