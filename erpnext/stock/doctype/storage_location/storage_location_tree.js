// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.treeview_settings["Storage Location"] = {
	get_tree_root: false,
	root_label: __("Storage Locations"),
	filters: [
		{
			fieldname: "warehouse",
			fieldtype: "Link",
			options: "Warehouse",
			label: __("Warehouse"),
		},
	],
	fields: [
		{ fieldtype: "Data", fieldname: "location_code", label: __("Location Code"), reqd: true },
		{ fieldtype: "Data", fieldname: "location_name", label: __("Location Name") },
		{ fieldtype: "Link", fieldname: "warehouse", options: "Warehouse", label: __("Warehouse") },
		{
			fieldtype: "Select",
			fieldname: "location_type",
			label: __("Location Type"),
			options: "Zone\nAisle\nRack\nBin\nStaging\nQC\nBulk\nPick Face",
			default: "Bin",
		},
		{
			fieldtype: "Check",
			fieldname: "is_group",
			label: __("Is Group"),
			description: __("Child nodes can be only created under 'Group' type nodes"),
		},
	],
	ignore_fields: ["parent_storage_location"],
};
