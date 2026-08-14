// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["SoD Violations"] = {
	filters: [
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nOpen\nMitigated\nAccepted\nResolved",
			default: "Open",
		},
		{
			fieldname: "risk_level",
			label: __("Risk Level"),
			fieldtype: "Select",
			options: "\nHigh\nMedium\nLow",
		},
		{
			fieldname: "user",
			label: __("User"),
			fieldtype: "Link",
			options: "User",
		},
	],
};
