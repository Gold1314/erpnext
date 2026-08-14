// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("EDI Transmission Profile", {
	refresh(frm) {
		if (frm.doc.ubl_profile === "xrechnung-3" && !frm.doc.seller_endpoint_id) {
			frm.dashboard.set_headline(
				__(
					"XRechnung requires a seller electronic address — set the Seller Endpoint ID below."
				)
			);
		}
	},
});
