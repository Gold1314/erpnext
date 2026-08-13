// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Forecast Accuracy"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "sales_forecast",
			label: __("Sales Forecast"),
			fieldtype: "Link",
			options: "Sales Forecast",
			get_query: () => {
				return {
					filters: {
						docstatus: 1,
					},
				};
			},
		},
		{
			fieldname: "based_on",
			label: __("Actual Demand Based On"),
			fieldtype: "Select",
			options: ["Sales Order", "Sales Invoice", "Delivery Note"],
			default: "Sales Order",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -12),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data && data.is_summary) {
			value = "<strong>" + value + "</strong>";
		}
		return value;
	},
};
