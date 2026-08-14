// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Close Task Template"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Install Standard Monthly Close"), () => {
			frappe.call({
				method: "erpnext.accounts.doctype.close_task_template.close_task_template.create_default_close_template",
				freeze: true,
				freeze_message: __("Installing the Standard Monthly Close template..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("Default Close Template"),
						indicator: "green",
						message: res.created.length
							? __("Template {0} created.", [res.created[0].bold()])
							: __("Template {0} already exists.", [res.skipped[0].bold()]),
					});
					listview.refresh();
				},
			});
		});
	},
};
