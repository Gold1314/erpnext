// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Reconciliation Suggestions"] = {
	filters: [
		{
			fieldname: "bank_account",
			label: __("Bank Account"),
			fieldtype: "Link",
			options: "Bank Account",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -90),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "match_bucket" && data) {
			const colors = {
				Strong: "green",
				Good: "blue",
				Weak: "orange",
				None: "red",
			};
			const color = colors[data.match_bucket];
			if (color) {
				value = `<span class="indicator-pill ${color}">${value}</span>`;
			}
		}
		return value;
	},
};
