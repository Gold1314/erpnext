// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Lease Contract", {
	setup(frm) {
		frm.set_query("lease_liability_account", () => ({
			filters: {
				company: frm.doc.company,
				root_type: "Liability",
				is_group: 0,
			},
		}));
		frm.set_query("interest_expense_account", () => ({
			filters: {
				company: frm.doc.company,
				root_type: "Expense",
				is_group: 0,
			},
		}));
		frm.set_query("short_term_expense_account", () => ({
			filters: {
				company: frm.doc.company,
				root_type: "Expense",
				is_group: 0,
			},
		}));
		frm.set_query("payment_account", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
			},
		}));
		frm.set_query("asset_item", () => ({
			filters: {
				is_fixed_asset: 1,
				is_stock_item: 0,
				disabled: 0,
			},
		}));
		frm.set_query("cost_center", () => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
			},
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Generate Payments"), () => {
				if (!frm.doc.payment_amount || !frm.doc.payment_frequency) {
					frappe.msgprint(__("Set Payment Frequency and Payment Amount first."));
					return;
				}
				frm.call({
					doc: frm.doc,
					method: "generate_payments",
					freeze: true,
					freeze_message: __("Generating payment rows..."),
				}).then(() => frm.reload_doc());
			});
		}

		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Post Entries"), () => {
				frappe.prompt(
					{
						fieldname: "until_date",
						fieldtype: "Date",
						label: __("Post periods ending on or before"),
						default: frappe.datetime.get_today(),
						reqd: 1,
					},
					(values) => {
						frm.call({
							doc: frm.doc,
							method: "post_monthly_entries",
							args: { until_date: values.until_date },
							freeze: true,
							freeze_message: __("Posting lease journal entries..."),
						}).then(() => frm.reload_doc());
					},
					__("Post Lease Entries")
				);
			});

			if (frm.doc.is_short_term) {
				frm.set_intro(
					__("Short-term lease: payments are expensed as incurred; no ROU asset or lease liability is recognized."),
					"blue"
				);
			} else {
				frm.set_intro(
					__("Initial lease liability {0}, initial right-of-use asset {1}.", [
						format_currency(frm.doc.initial_liability, frm.doc.currency),
						format_currency(frm.doc.initial_rou, frm.doc.currency),
					]),
					"blue"
				);
			}
		}
	},
});
