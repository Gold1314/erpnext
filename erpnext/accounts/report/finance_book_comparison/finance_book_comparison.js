// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Finance Book Comparison"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.year_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "finance_book_1",
			label: __("Finance Book 1"),
			fieldtype: "Link",
			options: "Finance Book",
			reqd: 1,
		},
		{
			fieldname: "finance_book_2",
			label: __("Finance Book 2"),
			fieldtype: "Link",
			options: "Finance Book",
			description: __("Leave empty to compare against the default book (untagged entries only)."),
		},
		{
			fieldname: "root_type",
			label: __("Root Type"),
			fieldtype: "Select",
			options: "\nAsset\nLiability\nEquity\nIncome\nExpense",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		if (data && data.is_group_row) {
			if (column.fieldname === "account") {
				return `<b>${__(data.root_type)}</b>`;
			}
			return `<b>${default_formatter(value, row, column, data)}</b>`;
		}
		return default_formatter(value, row, column, data);
	},

	tree: true,
	name_field: "account",
	initial_depth: 2,
};
