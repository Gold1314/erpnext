// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["AI Document Extraction"] = {
	add_fields: ["status", "supplier", "grand_total", "purchase_invoice"],

	get_indicator(doc) {
		const indicators = {
			"Pending Extraction": ["Pending Extraction", "orange", "status,=,Pending Extraction"],
			Extracted: ["Extracted", "blue", "status,=,Extracted"],
			"Needs Review": ["Needs Review", "yellow", "status,=,Needs Review"],
			"Invoice Created": ["Invoice Created", "green", "status,=,Invoice Created"],
			Failed: ["Failed", "red", "status,=,Failed"],
			Rejected: ["Rejected", "gray", "status,=,Rejected"],
		};
		const indicator = indicators[doc.status];
		return indicator ? [__(indicator[0]), indicator[1], indicator[2]] : undefined;
	},

	onload(listview) {
		listview.page.add_inner_button(__("AI Extraction Queue"), () => {
			frappe.set_route("query-report", "AI Extraction Queue");
		});
	},
};
