// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Anomaly Finding"] = {
	add_fields: ["status", "severity"],
	get_indicator(doc) {
		if (doc.status === "Open") {
			const by_severity = {
				High: ["red", "severity,=,High"],
				Medium: ["orange", "severity,=,Medium"],
				Low: ["yellow", "severity,=,Low"],
			};
			const [color, filter] = by_severity[doc.severity] || ["grey", ""];
			return [__("Open ({0})", [__(doc.severity)]), color, `status,=,Open|${filter}`];
		}

		const by_status = {
			Investigating: "blue",
			"Confirmed Issue": "purple",
			"False Positive": "grey",
			Resolved: "green",
		};
		return [__(doc.status), by_status[doc.status] || "grey", `status,=,${doc.status}`];
	},
};
