// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Consolidated Statement with Eliminations"] = {
	filters: [
		{
			fieldname: "consolidation_run",
			label: __("Consolidation Run"),
			fieldtype: "Link",
			options: "Consolidation Run",
			reqd: 1,
			get_query() {
				return { filters: { status: ["in", ["Computed", "Finalized"]] } };
			},
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data && data.is_section_header) {
			value = `<span style="font-weight: bold">${value}</span>`;
		}
		if (data && data.is_total_row) {
			value = `<span style="font-weight: 600">${value}</span>`;
		}
		return value;
	},
};
