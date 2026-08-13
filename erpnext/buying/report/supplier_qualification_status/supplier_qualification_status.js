// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Supplier Qualification Status"] = {
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
			fieldname: "supplier_group",
			label: __("Supplier Group"),
			fieldtype: "Link",
			options: "Supplier Group",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nQualified\nRejected\nExpired",
		},
		{
			fieldname: "risk_tier",
			label: __("Risk Tier"),
			fieldtype: "Select",
			options: "\nLow\nMedium\nHigh",
		},
		{
			fieldname: "expiring_within_days",
			label: __("Documents Expiring Within (Days)"),
			fieldtype: "Int",
			default: 30,
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && column.fieldname === "risk_tier") {
			const colors = { Low: "green", Medium: "orange", High: "red" };
			const color = colors[data.risk_tier];
			if (color) value = `<span style="color: var(--text-on-${color}, inherit)">${value}</span>`;
		}
		if (data && column.fieldname === "status" && data.status !== "Qualified") {
			value = `<span class="text-danger">${value}</span>`;
		}
		if (data && column.fieldname === "mandatory_docs_ok" && data.mandatory_docs_ok === __("No")) {
			value = `<span class="text-danger">${value}</span>`;
		}
		if (data && column.fieldname === "days_to_expiry" && data.days_to_expiry !== null) {
			if (data.days_to_expiry < 0) value = `<span class="text-danger">${value}</span>`;
			else if (data.days_to_expiry <= 30) value = `<span class="text-warning">${value}</span>`;
		}

		return value;
	},
};
