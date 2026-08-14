// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("EDI Transmission Log", {
	refresh(frm) {
		if (frm.doc.file_url) {
			frm.add_custom_button(__("Download XML"), () => {
				window.open(frm.doc.file_url);
			});
		}

		if (["Generated", "Queued"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Transmit"), () => {
				frappe.call({
					method: "erpnext.edi.ubl.api.transmit",
					args: { log_name: frm.doc.name },
					freeze: true,
					freeze_message: __("Transmitting..."),
					callback: () => frm.reload_doc(),
				});
			}).addClass("btn-primary");
		}
	},
});
