// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Agent Policy"] = {
	add_fields: ["enabled", "is_default"],

	get_indicator(doc) {
		if (!doc.enabled) return [__("Disabled"), "grey", "enabled,=,0"];
		if (doc.is_default) return [__("Enforced"), "green", "is_default,=,1"];
		return [__("Enabled"), "blue", "enabled,=,1"];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Install Shipped Default"), () => {
			frappe.confirm(
				__(
					"Create the shipped safe policy: a curated read allowlist, drafts only on transaction doctypes, and submit/destructive denied everywhere. An existing policy of the same name is left untouched."
				),
				() => {
					frappe.call({
						method: "erpnext.utilities.doctype.agent_policy.agent_policy.install_default_policy",
						freeze: true,
						freeze_message: __("Installing default agent policy..."),
						callback(r) {
							if (!r.message) return;
							frappe.msgprint({
								title: __("Default Agent Policy"),
								indicator: r.message.status === "created" ? "green" : "orange",
								message: r.message.message,
							});
							listview.refresh();
						},
					});
				}
			);
		});
	},
};
