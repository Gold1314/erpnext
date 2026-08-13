// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.provide("erpnext.ai_extraction");

const AI_STATUS_INDICATORS = {
	"Pending Extraction": "orange",
	Extracted: "blue",
	"Needs Review": "yellow",
	"Invoice Created": "green",
	Failed: "red",
	Rejected: "gray",
};

frappe.ui.form.on("AI Document Extraction", {
	refresh(frm) {
		frm.set_df_property("items", "cannot_add_rows", frm.doc.status === "Invoice Created");

		erpnext.ai_extraction.set_indicator(frm);
		erpnext.ai_extraction.show_review_headline(frm);
		erpnext.ai_extraction.add_buttons(frm);
	},

	status(frm) {
		erpnext.ai_extraction.set_indicator(frm);
	},
});

erpnext.ai_extraction = {
	set_indicator(frm) {
		if (frm.doc.__islocal) return;
		frm.page.set_indicator(__(frm.doc.status), AI_STATUS_INDICATORS[frm.doc.status] || "gray");
	},

	show_review_headline(frm) {
		// what needs a human is why anyone opens this form — surface it on top
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

		const parts = [];

		if (frm.doc.provider_used) {
			parts.push(
				`<div class="text-muted small">${__("Extracted by {0} ({1})", [
					frappe.utils.escape_html(frm.doc.model_used || __("unknown model")),
					frappe.utils.escape_html(frm.doc.provider_used),
				])}</div>`
			);
		}

		if (frm.doc.needs_review_fields) {
			const fields = frm.doc.needs_review_fields
				.split("\n")
				.filter(Boolean)
				.map((field) => `<span class="indicator-pill yellow">${frappe.utils.escape_html(field)}</span>`)
				.join(" ");
			parts.push(`<b>${__("Fields needing review")}</b>: ${fields}`);
		}

		if (frm.doc.warnings && frm.doc.status !== "Extracted") {
			const lines = frm.doc.warnings
				.split("\n")
				.filter(Boolean)
				.slice(0, 6)
				.map((line) => `<li>${frappe.utils.escape_html(line)}</li>`)
				.join("");
			parts.push(`<b>${__("Warnings")}</b><ul class="mb-0">${lines}</ul>`);
		}

		if (parts.length) {
			frm.dashboard.set_headline(
				parts.join("<br>"),
				frm.doc.status === "Failed" ? "red" : frm.doc.status === "Needs Review" ? "yellow" : "blue"
			);
		}
	},

	call(frm, method, freeze_message) {
		frappe.call({
			method: `erpnext.ai.api.${method}`,
			args: { name: frm.doc.name },
			freeze: true,
			freeze_message,
			callback: () => frm.reload_doc(),
		});
	},

	add_buttons(frm) {
		if (frm.doc.__islocal) return;

		if (["Pending Extraction", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Extract"), () => {
				erpnext.ai_extraction.call(frm, "extract_document", __("Asking the model..."));
			}).addClass("btn-primary");
		}

		if (["Extracted", "Needs Review"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Re-extract"), () => {
				frappe.confirm(
					__("Re-extraction overwrites the extracted fields and lines (your Supplier and Item corrections on rows will be lost). Continue?"),
					() => erpnext.ai_extraction.call(frm, "extract_document", __("Asking the model..."))
				);
			});

			frm.add_custom_button(__("Resolve Masters"), () => {
				erpnext.ai_extraction.call(frm, "resolve_masters", __("Resolving supplier and accounts..."));
			});
		}

		if (
			["Extracted", "Needs Review"].includes(frm.doc.status) &&
			!frm.doc.purchase_invoice
		) {
			frm.add_custom_button(__("Create Purchase Invoice"), () => {
				frappe.confirm(
					__(
						"A <b>draft</b> Purchase Invoice will be created. It is never submitted automatically — review it before posting. Continue?"
					),
					() => {
						frappe.call({
							method: "erpnext.ai.api.create_purchase_invoice",
							args: { name: frm.doc.name },
							freeze: true,
							freeze_message: __("Creating draft Purchase Invoice..."),
							callback(r) {
								if (r.message && r.message.purchase_invoice) {
									frappe.set_route("Form", "Purchase Invoice", r.message.purchase_invoice);
								} else {
									frm.reload_doc();
								}
							},
						});
					}
				);
			}).addClass("btn-primary");
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
							"warnings",
							[frm.doc.warnings, __("Rejected: {0}", [values.reason])]
								.filter(Boolean)
								.join("\n")
						);
						frm.save();
					},
					__("Reject Document")
				);
			});
		}
	},
};
