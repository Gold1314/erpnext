// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Finance Book Adjustment", {
	setup(frm) {
		frm.set_query("cost_center", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Compute Adjustment Lines"), () => {
				frm.call({
					doc: frm.doc,
					method: "compute",
					freeze: true,
					freeze_message: __("Computing adjustment lines from policy rules..."),
				}).then((r) => {
					frm.reload_doc();
					if (r.exc || !r.message) return;
					frappe.show_alert({
						message: __("{0} lines computed (debit {1} / credit {2}).", [
							r.message.lines,
							format_currency(r.message.total_debit),
							format_currency(r.message.total_credit),
						]),
						indicator: "green",
					});
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.journal_entry) {
			frm.add_custom_button(__("Journal Entry"), () => {
				frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
			}, __("View"));
		}

		if (frm.doc.docstatus === 0) {
			const message =
				frm.doc.status === "Computed"
					? __(
							"Lines are computed from the common (untagged) GL layer for {0} to {1}. Submitting posts one Journal Entry tagged with finance book {2}.",
							[frm.doc.from_date, frm.doc.to_date, frm.doc.finance_book]
					  )
					: __(
							"Pick the company, finance book and period, then Compute Adjustment Lines to preview the book-specific Journal Entry."
					  );
			frm.set_intro(message, "blue");
		}
	},
});
