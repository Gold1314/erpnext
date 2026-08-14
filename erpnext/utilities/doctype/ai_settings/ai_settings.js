// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		if (frm.doc.provider_type && frm.doc.model) {
			frm.dashboard.set_headline(
				__("Extractions will run on {0} via {1}.", [
					frappe.utils.escape_html(frm.doc.model),
					frappe.utils.escape_html(
						frm.doc.base_url || (frm.doc.provider_type === "Anthropic" ? "api.anthropic.com" : "")
					) || __("(no base URL set)"),
				])
			);
		} else {
			frm.dashboard.set_headline(
				__("Set Provider Type and Model to enable AI document extraction.")
			);
		}
	},
});
