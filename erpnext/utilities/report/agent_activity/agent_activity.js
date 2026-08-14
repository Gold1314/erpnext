// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Agent Activity"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Datetime",
			default: frappe.datetime.add_days(frappe.datetime.now_datetime(), -7),
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Datetime",
			default: frappe.datetime.now_datetime(),
		},
		{
			fieldname: "user",
			label: __("User"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "tool",
			label: __("Tool"),
			fieldtype: "Data",
		},
		{
			fieldname: "risk_level",
			label: __("Risk Level"),
			fieldtype: "Select",
			options: ["", "READ", "DRAFT_WRITE", "SUBMIT", "DESTRUCTIVE"],
		},
		{
			fieldname: "decision",
			label: __("Decision"),
			fieldtype: "Select",
			options: ["", "Allowed", "Denied"],
		},
		{
			fieldname: "outcome",
			label: __("Outcome"),
			fieldtype: "Select",
			options: ["", "Success", "Error", "Denied"],
		},
		{
			fieldname: "group_by",
			label: __("Chart By"),
			fieldtype: "Select",
			options: ["Tool", "Decision", "Risk Level", "User"],
			default: "Tool",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		if (column.fieldname === "decision") {
			const color = data.decision === "Denied" ? "red" : "green";
			value = `<span class="indicator-pill ${color}">${__(data.decision || "")}</span>`;
		}

		if (column.fieldname === "outcome") {
			const colors = { Success: "green", Error: "orange", Denied: "red" };
			const color = colors[data.outcome] || "gray";
			value = `<span class="indicator-pill ${color}">${__(data.outcome || "")}</span>`;
		}

		// a write is the row an auditor cares about most — make it stand out
		if (column.fieldname === "risk_level" && data.risk_level && data.risk_level !== "READ") {
			value = `<span style="font-weight: 600;">${value}</span>`;
		}

		return value;
	},
};
