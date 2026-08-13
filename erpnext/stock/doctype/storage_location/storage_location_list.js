// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Storage Location"] = {
	add_fields: ["disabled", "is_group", "location_type"],
	get_indicator(doc) {
		if (doc.disabled) {
			return [__("Disabled"), "gray", "disabled,=,1"];
		}
		if (doc.is_group) {
			return [__("Group"), "blue", "is_group,=,1"];
		}
		return [__(doc.location_type || "Bin"), "green", "disabled,=,0"];
	},
};
