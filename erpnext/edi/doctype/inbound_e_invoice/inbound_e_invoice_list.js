// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Inbound E-Invoice"] = {
	add_fields: ["status", "supplier", "grand_total", "purchase_invoice"],

	get_indicator(doc) {
		const indicators = {
			"Pending Review": ["Pending Review", "orange", "status,=,Pending Review"],
			Matched: ["Matched", "blue", "status,=,Matched"],
			Exception: ["Exception", "red", "status,=,Exception"],
			"Invoice Created": ["Invoice Created", "green", "status,=,Invoice Created"],
			Rejected: ["Rejected", "gray", "status,=,Rejected"],
		};
		const indicator = indicators[doc.status];
		return indicator ? [__(indicator[0]), indicator[1], indicator[2]] : undefined;
	},

	onload(listview) {
		listview.page.add_inner_button(__("Inbound E-Invoice Queue"), () => {
			frappe.set_route("query-report", "Inbound E-Invoice Queue");
		});
	},
};
