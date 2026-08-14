// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

const KPI_STATUS_COLOURS = {
	Green: "green",
	Amber: "orange",
	Red: "red",
	Unknown: "gray",
};

frappe.query_reports["KPI Scorecard"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "period_start",
			label: __("Period Start"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		},
		{
			fieldname: "period_end",
			label: __("Period End"),
			fieldtype: "Date",
			default: frappe.datetime.month_end(),
			reqd: 1,
		},
		{
			fieldname: "category",
			label: __("Category"),
			fieldtype: "Select",
			options: [
				"",
				"Liquidity",
				"Receivables",
				"Profitability",
				"Growth",
				"Operations",
				"Governance",
			].join("\n"),
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && data.is_group) {
			return `<span style="font-weight:600">${value}</span>`;
		}

		if (column.fieldname === "status" && data && data.status) {
			const colour = KPI_STATUS_COLOURS[data.status] || "gray";
			return `<span class="indicator-pill ${colour}">${__(data.status)}</span>`;
		}

		if (["change", "pct_change"].includes(column.fieldname) && data && data.change !== undefined) {
			if (data.direction_is_good === true) {
				return `<span style="color: var(--green-600)">${value}</span>`;
			}
			if (data.direction_is_good === false && data.change && data.change !== __("n/a")) {
				return `<span style="color: var(--gray-700)">${value}</span>`;
			}
		}

		return value;
	},

	onload(report) {
		report.page.add_inner_button(__("Save Snapshot"), () => {
			const filters = report.get_values();
			if (!filters.company) return;

			frappe.call({
				method: "erpnext.analytics.api.snapshot_kpis",
				args: {
					company: filters.company,
					period_start: filters.period_start,
					period_end: filters.period_end,
				},
				freeze: true,
				freeze_message: __("Saving KPI snapshot..."),
				callback(r) {
					if (r.exc || !r.message) return;
					const res = r.message;
					frappe.msgprint({
						title: __("KPI Snapshot"),
						indicator: "green",
						message: __("{0} snapshots created and {1} updated for {2}.", [
							res.created,
							res.updated,
							res.company,
						]),
					});
				},
			});
		});

		report.page.add_inner_button(__("Metric Catalog"), () => {
			frappe.set_route("List", "Metric Definition");
		});
	},
};
