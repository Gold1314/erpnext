// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Anomaly Detection Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Run Scan Now"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Run Anomaly Scan"),
				fields: [
					{
						fieldname: "company",
						label: __("Company"),
						fieldtype: "Link",
						options: "Company",
						description: __("Leave empty to scan all companies"),
					},
				],
				primary_action_label: __("Run"),
				primary_action(values) {
					dialog.hide();
					frappe.call({
						method: "erpnext.accounts.anomaly.scanner.run_scan_now",
						args: { company: values.company || null },
						freeze: true,
						freeze_message: __("Running anomaly scan..."),
						callback(r) {
							if (r.exc || !r.message) return;
							const res = r.message;
							frappe.msgprint({
								title: __("Anomaly Scan Complete"),
								indicator: res.total ? "orange" : "green",
								message: __("{0} new findings across {1} companies.", [
									res.total,
									(res.companies || []).length,
								]),
							});
						},
					});
				},
			});
			dialog.show();
		});

		frm.add_custom_button(__("View Findings"), () => {
			frappe.set_route("List", "Anomaly Finding", { status: "Open" });
		});
	},
});
