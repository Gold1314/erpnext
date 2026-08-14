// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.provide("erpnext.inbound_einvoice");

const STATUS_INDICATORS = {
	"Pending Review": "orange",
	Matched: "blue",
	Exception: "red",
	"Invoice Created": "green",
	Rejected: "gray",
};

frappe.ui.form.on("Inbound E-Invoice", {
	refresh(frm) {
		frm.set_df_property("items", "cannot_add_rows", frm.doc.status === "Invoice Created");

		erpnext.inbound_einvoice.set_indicator(frm);
		erpnext.inbound_einvoice.show_exceptions(frm);
		erpnext.inbound_einvoice.add_buttons(frm);
	},

	status(frm) {
		erpnext.inbound_einvoice.set_indicator(frm);
	},
});

erpnext.inbound_einvoice = {
	set_indicator(frm) {
		if (frm.doc.__islocal) return;
		frm.page.set_indicator(__(frm.doc.status), STATUS_INDICATORS[frm.doc.status] || "gray");
	},

	show_exceptions(frm) {
		// Exceptions are why a human is looking at this document at all —
		// surface them above the form instead of at the bottom of a section.
		frm.dashboard.clear_headline();

		if (frm.doc.status === "Invoice Created" && frm.doc.purchase_invoice) {
			frm.dashboard.set_headline(
				__("Draft Purchase Invoice {0} was created from this document.", [
					frappe.utils.get_form_link(
						"Purchase Invoice",
						frm.doc.purchase_invoice,
						true,
						frm.doc.purchase_invoice
					),
				])
			);
			return;
		}

		if (frm.doc.exceptions) {
			const lines = frm.doc.exceptions
				.split("\n")
				.filter(Boolean)
				.map((line) => `<li>${frappe.utils.escape_html(line)}</li>`)
				.join("");
			frm.dashboard.set_headline(
				`<b>${__("Exceptions")}</b><ul class="mb-0">${lines}</ul>`,
				"red"
			);
		}
	},

	add_buttons(frm) {
		if (frm.doc.__islocal) return;

		if (frm.doc.status !== "Invoice Created") {
			frm.add_custom_button(__("Match Against PO"), () => {
				frappe.call({
					method: "erpnext.edi.inbound.api.match_against_po",
					args: { inbound_einvoice: frm.doc.name },
					freeze: true,
					freeze_message: __("Matching against open Purchase Orders..."),
					callback: () => frm.reload_doc(),
				});
			});
		}

		if (["Pending Review", "Matched"].includes(frm.doc.status) && !frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Create Purchase Invoice"), () => {
				frappe.confirm(
					__(
						"A <b>draft</b> Purchase Invoice will be created. It is never submitted automatically — review it before posting. Continue?"
					),
					() => {
						frappe.call({
							method: "erpnext.edi.inbound.api.create_purchase_invoice",
							args: { inbound_einvoice: frm.doc.name },
							freeze: true,
							freeze_message: __("Creating draft Purchase Invoice..."),
							callback(r) {
								if (r.message && r.message.purchase_invoice) {
									frappe.set_route(
										"Form",
										"Purchase Invoice",
										r.message.purchase_invoice
									);
								} else {
									frm.reload_doc();
								}
							},
						});
					}
				);
			}).addClass("btn-primary");
		}

		if (frm.doc.source_file) {
			frm.add_custom_button(__("Download Source XML"), () => {
				window.open(frm.doc.source_file);
			});
		}

		if (!["Invoice Created", "Rejected"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Reject"), () => {
				frappe.prompt(
					{
						fieldname: "reason",
						label: __("Reason"),
						fieldtype: "Small Text",
						reqd: 1,
					},
					(values) => {
						frm.set_value("status", "Rejected");
						frm.set_value(
							"exceptions",
							[frm.doc.exceptions, __("Rejected: {0}", [values.reason])]
								.filter(Boolean)
								.join("\n")
						);
						frm.save();
					},
					__("Reject E-Invoice")
				);
			});
		}
	},
};
