// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Agent Action Log"] = {
	add_fields: ["decision", "outcome", "risk_level", "tool"],

	get_indicator(doc) {
		// a denial is the interesting row on this list — it is the guardrail
		// doing its job, or something trying to walk past it
		if (doc.decision === "Denied") {
			return [__("Denied"), "red", "decision,=,Denied"];
		}
		if (doc.outcome === "Error") {
			return [__("Error"), "orange", "outcome,=,Error"];
		}
		if (doc.risk_level && doc.risk_level !== "READ") {
			return [__("Wrote ({0})", [doc.risk_level]), "blue", `risk_level,=,${doc.risk_level}`];
		}
		return [__("Allowed"), "green", "decision,=,Allowed"];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Agent Activity Report"), () => {
			frappe.set_route("query-report", "Agent Activity");
		});

		listview.page.add_inner_button(__("Denied Calls"), () => {
			listview.filter_area.add([["Agent Action Log", "decision", "=", "Denied"]]);
		});
	},
};
