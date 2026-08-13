// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Metric Definition"] = {
	add_fields: ["enabled", "category", "direction"],

	get_indicator(doc) {
		if (!doc.enabled) {
			return [__("Disabled"), "grey", "enabled,=,0"];
		}
		return [__(doc.category || "Enabled"), "blue", `category,=,${doc.category}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Install Default Metrics"), () => {
			frappe.call({
				method: "erpnext.accounts.doctype.metric_definition.metric_definition.install_default_metrics",
				freeze: true,
				freeze_message: __("Installing the shipped KPI pack..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("KPI Pack"),
						indicator: res.created.length ? "green" : "blue",
						message: __("{0} of {1} metrics created, {2} already existed.", [
							res.created.length,
							res.catalog_size,
							res.skipped.length,
						]),
					});
					listview.refresh();
				},
			});
		});

		listview.page.add_inner_button(__("KPI Scorecard"), () => {
			frappe.set_route("query-report", "KPI Scorecard", {
				company: frappe.defaults.get_user_default("Company"),
			});
		});
	},
};
