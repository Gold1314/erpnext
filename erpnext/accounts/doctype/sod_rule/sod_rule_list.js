// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["SoD Rule"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Run Scan"), () => {
			frappe.call({
				method: "erpnext.accounts.doctype.sod_rule.sod_rule.scan_for_violations",
				freeze: true,
				freeze_message: __("Scanning for Segregation of Duties violations..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("SoD Scan Complete"),
						indicator: res.new_violations ? "orange" : "green",
						message: __(
							"Scanned {0} rules across {1} users: {2} new, {3} refreshed and {4} resolved violations.",
							[
								res.rules_scanned,
								res.users_scanned,
								res.new_violations,
								res.refreshed_violations,
								res.resolved_violations,
							]
						),
					});
					listview.refresh();
				},
			});
		});

		listview.page.add_inner_button(__("Install Default Rules"), () => {
			frappe.call({
				method: "erpnext.accounts.doctype.sod_rule.sod_rule.create_default_sod_rules",
				freeze: true,
				freeze_message: __("Installing default SoD rules..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("Default SoD Rules"),
						indicator: "green",
						message: __("{0} rules created, {1} already existed.", [
							res.created.length,
							res.skipped.length,
						]),
					});
					listview.refresh();
				},
			});
		});
	},
};
