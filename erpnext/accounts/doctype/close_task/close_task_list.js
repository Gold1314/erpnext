// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Close Task"] = {
	add_fields: ["status", "due_date"],
	get_indicator(doc) {
		const colors = {
			Pending: "gray",
			"In Progress": "blue",
			Blocked: "red",
			Completed: "green",
			Skipped: "orange",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};
