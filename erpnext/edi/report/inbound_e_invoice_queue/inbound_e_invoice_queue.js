// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Inbound E-Invoice Queue"] = {
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
				"Pending Review",
				"Matched",
				"Exception",
				"Invoice Created",
				"Rejected",
			],
		},
		{
			fieldname: "from_date",
			label: __("From Issue Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
		},
		{
			fieldname: "to_date",
			label: __("To Issue Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "only_exceptions",
			label: __("Only Rows With Exceptions"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		if (column.fieldname === "status") {
			const colors = {
				"Pending Review": "orange",
				Matched: "blue",
				Exception: "red",
				"Invoice Created": "green",
				Rejected: "gray",
			};
			const color = colors[data.status] || "gray";
			value = `<span class="indicator-pill ${color}">${__(data.status)}</span>`;
		}

		if (column.fieldname === "matched_pct") {
			const color = data.matched_pct >= 100 ? "green" : data.matched_pct > 0 ? "orange" : "red";
			value = `<span style="color: var(--text-on-light); font-weight: 500;">${value}</span>`;
			value = `<span class="indicator-pill-round ${color}"></span> ${value}`;
		}

		// an invoice we have been sitting on for more than a fortnight is
		// the whole point of the queue — make it impossible to miss
		if (column.fieldname === "ageing_days" && data.ageing_days > 14) {
			value = `<span style="color: var(--red-500); font-weight: 600;">${value}</span>`;
		}

		return value;
	},
};
