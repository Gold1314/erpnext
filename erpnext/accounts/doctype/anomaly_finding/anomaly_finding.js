// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Anomaly Finding", {
	refresh(frm) {
		if (frm.doc.reference_doctype && frm.doc.reference_name) {
			frm.add_custom_button(__("Open {0}", [__(frm.doc.reference_doctype)]), () => {
				frappe.set_route("Form", frm.doc.reference_doctype, frm.doc.reference_name);
			});
		}

		if (frm.doc.severity && frm.doc.status === "Open") {
			const colors = { High: "red", Medium: "orange", Low: "yellow" };
			frm.dashboard.set_headline_alert(
				`<span class="indicator ${colors[frm.doc.severity] || "grey"}">
					${__("Open {0} severity anomaly - review and set a resolution status.", [
						__(frm.doc.severity),
					])}
				</span>`
			);
		}
	},
});
