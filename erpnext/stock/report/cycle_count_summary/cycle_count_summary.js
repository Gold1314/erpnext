// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Cycle Count Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "program",
			label: __("Cycle Count Program"),
			fieldtype: "Link",
			options: "Cycle Count Program",
			get_query: () => {
				const company = frappe.query_report.get_filter_value("company");
				return company ? { filters: { company: company } } : {};
			},
		},
		{
			fieldname: "abc_class",
			label: __("ABC Class"),
			fieldtype: "Select",
			options: "\nA\nB\nC",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nClassified\nScheduled\nCounted",
		},
		{
			fieldname: "only_overdue",
			label: __("Only Overdue"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data && data.status) {
			const color = { Classified: "blue", Scheduled: "orange", Counted: "green" }[data.status];
			if (color) {
				value = `<span class="indicator-pill ${color}">${__(data.status)}</span>`;
			}
		}
		if (column.fieldname === "days_overdue" && data && data.days_overdue > 0) {
			value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
		}
		if (column.fieldname === "abc_class" && data && data.abc_class) {
			const color = { A: "red", B: "orange", C: "green" }[data.abc_class];
			if (color) {
				value = `<span class="indicator-pill ${color}">${data.abc_class}</span>`;
			}
		}
		return value;
	},
};
