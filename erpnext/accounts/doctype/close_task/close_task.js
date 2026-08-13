// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Close Task", {
	refresh(frm) {
		if (
			!frm.is_new() &&
			frm.doc.auto_verify &&
			!["Completed", "Skipped"].includes(frm.doc.status)
		) {
			frm.add_custom_button(__("Verify"), () => {
				if (frm.is_dirty()) {
					frappe.msgprint(__("Please save the task before verifying."));
					return;
				}
				frm.call({
					doc: frm.doc,
					method: "verify",
					freeze: true,
					freeze_message: __("Verifying against accounting records..."),
				}).then((r) => {
					if (!r || !r.message) return;
					frappe.msgprint({
						title: __("Verification Result"),
						indicator: r.message.ok ? "green" : "orange",
						message: r.message.message,
					});
					frm.reload_doc();
				});
			});
		}
	},
});
