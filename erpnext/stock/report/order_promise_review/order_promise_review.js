// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Order Promise Review"] = {
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
			label: __("From Delivery Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "to_date",
			label: __("To Delivery Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), 90),
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: () => {
				const company = frappe.query_report.get_filter_value("company");
				return { filters: { company: company, is_group: 0 } };
			},
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "only_at_risk",
			label: __("Only At Risk / Shortfall"),
			fieldtype: "Check",
			default: 1,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data && data.status) {
			const color = { OK: "green", "At Risk": "orange", Shortfall: "red" }[data.status];
			if (color) {
				value = `<span class="indicator-pill ${color}">${__(data.status)}</span>`;
			}
		}
		return value;
	},
};
