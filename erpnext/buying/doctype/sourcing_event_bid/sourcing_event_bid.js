// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sourcing Event Bid", {
	setup(frm) {
		frm.set_query("sourcing_event", () => ({ filters: { status: "Open", docstatus: 1 } }));
		frm.set_query("supplier", () => ({ filters: { disabled: 0 } }));
		frm.set_query("item_code", "items", () => {
			const items = (frm.doc.__event_items || []).map((row) => row.item_code);
			return items.length ? { filters: { name: ["in", items] } } : {};
		});
	},

	refresh(frm) {
		if (frm.doc.sourcing_event) {
			frm.add_custom_button(__("Sourcing Event"), () => {
				frappe.set_route("Form", "Sourcing Event", frm.doc.sourcing_event);
			});
		}
		if (frm.doc.docstatus === 0 && frm.doc.sourcing_event && !(frm.doc.items || []).length) {
			frm.add_custom_button(__("Fetch Event Items"), () => fetch_event_items(frm));
		}
	},

	sourcing_event(frm) {
		if (!frm.doc.sourcing_event) return;
		frappe.db.get_doc("Sourcing Event", frm.doc.sourcing_event).then((event) => {
			frm.doc.__event_items = event.items || [];
			if (!(frm.doc.items || []).length) fetch_event_items(frm, event);
		});
	},
});

frappe.ui.form.on("Sourcing Event Bid Item", {
	qty(frm, cdt, cdn) {
		set_amount(frm, cdt, cdn);
	},
	unit_price(frm, cdt, cdn) {
		set_amount(frm, cdt, cdn);
	},
	items_remove(frm) {
		set_total(frm);
	},
});

function set_amount(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.unit_price));
	set_total(frm);
}

function set_total(frm) {
	const total = (frm.doc.items || []).reduce((sum, row) => sum + flt(row.amount), 0);
	frm.set_value("total_amount", total);
}

function fetch_event_items(frm, event) {
	const apply = (doc) => {
		frm.clear_table("items");
		(doc.items || []).forEach((row) => {
			const child = frm.add_child("items");
			child.item_code = row.item_code;
			child.item_name = row.item_name;
			child.qty = row.qty;
		});
		frm.refresh_field("items");
	};

	if (event) {
		apply(event);
		return;
	}
	frappe.db.get_doc("Sourcing Event", frm.doc.sourcing_event).then(apply);
}
