// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Cash Flow Forecast"] = {
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
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), 90),
			reqd: 1,
		},
		{
			fieldname: "periodicity",
			label: __("Periodicity"),
			fieldtype: "Select",
			options: [
				{ value: "Daily", label: __("Daily") },
				{ value: "Weekly", label: __("Weekly") },
				{ value: "Monthly", label: __("Monthly") },
			],
			default: "Weekly",
			reqd: 1,
		},
		{
			fieldname: "include_sales_orders",
			label: __("Include Sales Orders"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "include_purchase_orders",
			label: __("Include Purchase Orders"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "receivable_delay_days",
			label: __("Receivable Delay (Days)"),
			fieldtype: "Int",
			default: 0,
		},
		{
			fieldname: "payable_delay_days",
			label: __("Payable Delay (Days)"),
			fieldtype: "Int",
			default: 0,
		},
		{
			fieldname: "pipeline_haircut_pct",
			label: __("Pipeline Haircut (%)"),
			fieldtype: "Int",
			default: 20,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data && data.bold) {
			value = $(`<span>${value}</span>`).css("font-weight", "bold").wrap("<p></p>").parent().html();
		}
		return value;
	},
};
