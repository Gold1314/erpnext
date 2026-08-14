// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Supplier Qualification Template"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Install Standard Questionnaire"), () => {
			frappe.call({
				method: "erpnext.buying.doctype.supplier_qualification_template.supplier_qualification_template.create_default_qualification_template",
				freeze: true,
				freeze_message: __("Installing the standard qualification questionnaire..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("Qualification Template"),
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
