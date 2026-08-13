// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Storage Location", {
	setup(frm) {
		frm.set_query("parent_storage_location", () => {
			const filters = { is_group: 1 };
			if (frm.doc.warehouse) {
				filters.warehouse = ["in", [frm.doc.warehouse, ""]];
			}
			if (!frm.doc.__islocal) {
				filters.name = ["!=", frm.doc.name];
			}
			return { filters };
		});

		frm.set_query("warehouse", () => {
			return { filters: { is_group: 0, disabled: 0 } };
		});
	},

	refresh(frm) {
		if (!frm.doc.__islocal) {
			frm.add_custom_button(
				__("Stock Ledger"),
				() => {
					frappe.route_options = { storage_location: frm.doc.name };
					frappe.set_route("query-report", "Stock Ledger");
				},
				__("View")
			);
		}
	},
});
