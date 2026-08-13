// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["AI Extraction Queue"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: [
				"",
				"Pending Extraction",
				"Extracted",
				"Needs Review",
				"Invoice Created",
				"Failed",
				"Rejected",
			],
		},
		{
			fieldname: "from_date",
			label: __("Received From"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
		},
		{
			fieldname: "to_date",
			label: __("Received To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "only_flagged",
			label: __("Only Rows With Flagged Fields"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		if (column.fieldname === "status") {
			const colors = {
				"Pending Extraction": "orange",
				Extracted: "blue",
				"Needs Review": "yellow",
				"Invoice Created": "green",
				Failed: "red",
				Rejected: "gray",
			};
			const color = colors[data.status] || "gray";
			value = `<span class="indicator-pill ${color}">${__(data.status)}</span>`;
		}

		if (column.fieldname === "flagged_fields" && data.flagged_fields > 0) {
			value = `<span style="color: var(--orange-500); font-weight: 600;">${value}</span>`;
		}

		// a document we have been sitting on for more than a fortnight is
		// the whole point of the queue — make it impossible to miss
		if (column.fieldname === "ageing_days" && data.ageing_days > 14) {
			value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
		}

		return value;
	},
};
