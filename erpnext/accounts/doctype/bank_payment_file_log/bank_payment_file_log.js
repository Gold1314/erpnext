// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bank Payment File Log", {
	refresh(frm) {
		if (frm.doc.file_url) {
			frm.add_custom_button(__("Download File"), () => {
				window.open(frm.doc.file_url);
			});
		}

		if (frm.doc.warnings) {
			frm.dashboard.add_comment(
				__("This file was generated with warnings. Review them before uploading."),
				"orange",
				true
			);
		}

		if (frm.doc.status === "Generated") {
			frm.add_custom_button(__("Mark Transmitted"), () => {
				const dialog = new frappe.ui.Dialog({
					title: __("Confirm Upload to Bank"),
					fields: [
						{
							fieldtype: "HTML",
							options: `<p class="text-muted">${__(
								"Confirm that this file was uploaded to the bank portal. Check that the portal shows {0} transaction(s) totalling {1}.",
								[
									frm.doc.number_of_transactions,
									format_currency(frm.doc.control_sum, frm.doc.currency),
								]
							)}</p>`,
						},
						{
							fieldtype: "Small Text",
							fieldname: "remarks",
							label: __("Remarks"),
							description: __("e.g. the bank's batch reference"),
						},
					],
					primary_action_label: __("Mark Transmitted"),
					primary_action(values) {
						frappe.call({
							method: "erpnext.accounts.payments_iso20022.api.mark_transmitted",
							args: { log_name: frm.doc.name, remarks: values.remarks },
							freeze: true,
							callback: () => {
								dialog.hide();
								frm.reload_doc();
							},
						});
					},
				});
				dialog.show();
			}).addClass("btn-primary");

			frm.add_custom_button(__("Mark Failed"), () => {
				frappe.prompt(
					{
						fieldtype: "Small Text",
						fieldname: "remarks",
						label: __("Reason"),
						reqd: 1,
					},
					(values) => {
						frm.call("mark_failed", { remarks: values.remarks }).then(() =>
							frm.reload_doc()
						);
					},
					__("Mark Failed"),
					__("Confirm")
				);
			});
		}
	},
});
