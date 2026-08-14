// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Anomaly Findings Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "check_key",
			label: __("Check"),
			fieldtype: "Select",
			options: [
				"",
				{ value: "duplicate_invoice", label: __("Duplicate Invoice") },
				{ value: "account_outlier", label: __("Account Outlier") },
				{ value: "rare_combination", label: __("Rare Combination") },
				{ value: "suspicious_posting", label: __("Suspicious Posting") },
				{ value: "benford_deviation", label: __("Benford Deviation") },
			],
		},
		{
			fieldname: "severity",
			label: __("Severity"),
			fieldtype: "Select",
			options: "\nHigh\nMedium\nLow",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nOpen\nInvestigating\nConfirmed Issue\nFalse Positive\nResolved",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
	],
};
