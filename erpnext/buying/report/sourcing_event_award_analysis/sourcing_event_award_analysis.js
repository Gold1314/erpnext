// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Sourcing Event Award Analysis"] = {
	filters: [
		{
			fieldname: "sourcing_event",
			label: __("Sourcing Event"),
			fieldtype: "Link",
			options: "Sourcing Event",
			reqd: 1,
			get_query() {
				return { filters: { docstatus: 1 } };
			},
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && column.fieldname === "rank" && data.rank === 1) {
			value = `<b>${value}</b>`;
		}
		if (data && column.fieldname === "scenarios" && data.scenarios) {
			value = `<span class="text-success">${value}</span>`;
		}

		return value;
	},
};
