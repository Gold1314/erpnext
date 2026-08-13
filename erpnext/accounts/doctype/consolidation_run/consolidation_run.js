// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Consolidation Run", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.docstatus === 0) {
			frm.set_intro(
				__(
					"Consolidation is a reporting layer: computing and submitting a run never posts to the General Ledger."
				)
			);
			frm.add_custom_button(__("Compute"), () => {
				frm.call({
					doc: frm.doc,
					method: "compute",
					freeze: true,
					freeze_message: __("Computing consolidation..."),
					callback(r) {
						frm.reload_doc();
						const result = r.message || {};
						if ((result.exceptions || []).length) {
							frappe.msgprint({
								title: __("Elimination Exceptions"),
								indicator: "orange",
								message: frappe.utils.escape_html(result.exceptions.join("\n")).replace(/\n/g, "<br>"),
							});
						} else {
							frappe.show_alert({
								message: __("Consolidation computed with no exceptions."),
								indicator: "green",
							});
						}
					},
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Computed" || frm.doc.status === "Finalized") {
			frm.add_custom_button(__("Open Report"), () => {
				frappe.set_route("query-report", "Consolidated Statement with Eliminations", {
					consolidation_run: frm.doc.name,
				});
			});
		}

		if (frm.doc.exceptions) {
			frm.dashboard.clear_comment();
			frm.dashboard.add_comment(
				__("This run has exceptions or warnings — see the Exceptions section."),
				"orange",
				true
			);
		}
	},

	to_date(frm) {
		if (!frm.doc.report_date) {
			frm.set_value("report_date", frm.doc.to_date);
		}
	},
});
