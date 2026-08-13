// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cycle Count Program", {
	setup(frm) {
		frm.set_query("warehouse", () => {
			return { filters: { company: frm.doc.company, is_group: 0, disabled: 0 } };
		});
	},

	refresh(frm) {
		if (frm.doc.__islocal) {
			return;
		}

		frm.add_custom_button(
			__("Classify Items (ABC)"),
			() => frm.trigger("run_classification"),
			__("Actions")
		);

		if (frm.doc.enabled) {
			frm.add_custom_button(
				__("Generate Count Sheets"),
				() => frm.trigger("run_generate_counts"),
				__("Actions")
			);
		}

		frm.add_custom_button(__("Sync Count Status"), () => frm.trigger("run_sync_counts"), __("Actions"));

		frm.add_custom_button(
			__("Cycle Count Summary"),
			() => {
				frappe.route_options = { program: frm.doc.name };
				frappe.set_route("query-report", "Cycle Count Summary");
			},
			__("View")
		);
	},

	run_classification(frm) {
		frm.call({
			doc: frm.doc,
			method: "classify_items",
			freeze: true,
			freeze_message: __("Classifying items by consumption velocity..."),
			callback: (r) => {
				if (r.message && r.message.message) {
					frappe.msgprint(r.message.message);
				}
				frm.reload_doc();
			},
		});
	},

	run_generate_counts(frm) {
		frm.call({
			doc: frm.doc,
			method: "generate_counts",
			freeze: true,
			freeze_message: __("Generating draft count sheets..."),
			callback: (r) => {
				if (r.message && r.message.message) {
					frappe.msgprint(r.message.message);
				}
			},
		});
	},

	run_sync_counts(frm) {
		frm.call({
			doc: frm.doc,
			method: "sync_counts",
			freeze: true,
			callback: (r) => {
				if (r.message && r.message.message) {
					frappe.msgprint(r.message.message);
				}
			},
		});
	},
});
