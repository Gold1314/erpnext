// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metric Definition", {
	refresh(frm) {
		frm.trigger("set_direction_intro");

		if (!frm.is_new()) {
			frm.add_custom_button(__("KPI Scorecard"), () => {
				frappe.set_route("query-report", "KPI Scorecard", {
					company: frm.doc.company || frappe.defaults.get_user_default("Company"),
				});
			});
		}
	},

	direction(frm) {
		frm.trigger("set_direction_intro");
	},

	set_direction_intro(frm) {
		if (!frm.doc.direction) {
			frm.set_intro("");
			return;
		}

		const intros = {
			higher_is_better: __(
				"Higher is better: Green when the value is at or above the Green Boundary, Amber when at or above the Amber Boundary."
			),
			lower_is_better: __(
				"Lower is better: Green when the value is at or below the Green Boundary, Amber when at or below the Amber Boundary."
			),
			neutral: __(
				"This metric is neutral - it has no better or worse side, so it is never graded and always reports as Unknown."
			),
		};

		frm.set_intro(intros[frm.doc.direction] || "", frm.doc.direction === "neutral" ? "blue" : "green");
	},
});
