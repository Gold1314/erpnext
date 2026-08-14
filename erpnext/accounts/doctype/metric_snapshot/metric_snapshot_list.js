// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Metric Snapshot"] = {
	add_fields: ["status", "value", "unit"],

	get_indicator(doc) {
		const by_status = {
			Green: "green",
			Amber: "orange",
			Red: "red",
		};
		const color = by_status[doc.status] || "grey";
		const label = doc.status || __("Unknown");
		return [__(label), color, `status,=,${doc.status || "Unknown"}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("KPI Scorecard"), () => {
			frappe.set_route("query-report", "KPI Scorecard", {
				company: frappe.defaults.get_user_default("Company"),
			});
		});
	},
};
