// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["EDI Transmission Log"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			Generated: "orange",
			Queued: "blue",
			Transmitted: "green",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
	hide_name_column: true,
};
