// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Bank Payment File Log"] = {
	add_fields: ["status", "warnings"],
	get_indicator(doc) {
		if (doc.status === "Generated" && doc.warnings) {
			return [__("Generated (with warnings)"), "orange", "status,=,Generated"];
		}
		const colors = {
			Generated: "blue",
			Transmitted: "green",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
	hide_name_column: true,
};
