// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Revenue Contract", {
	refresh(frm) {
		frm.trigger("show_allocation_summary");

		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Load from Source"), () => {
				frm.call({
					doc: frm.doc,
					method: "load_from_source",
					freeze: true,
					freeze_message: __("Loading obligations from the source document..."),
				}).then(() => frm.reload_doc());
			});
		}

		if (frm.doc.docstatus === 1 && frm.doc.status === "Active") {
			frm.add_custom_button(__("Post Recognition"), () => frm.trigger("post_recognition"));
			frm.add_custom_button(__("Mark Obligation Satisfied"), () =>
				frm.trigger("mark_obligation_satisfied")
			);
		}
	},

	show_allocation_summary(frm) {
		if (frm.is_new() || !(frm.doc.obligations || []).length) return;

		const currency = frappe.get_doc(":Company", frm.doc.company)?.default_currency;
		const fmt = (v) => format_currency(v, currency);
		const recognized = (frm.doc.obligations || []).reduce(
			(sum, o) => sum + (o.recognized_amount || 0),
			0
		);
		const pending = (frm.doc.recognition_plan || []).filter((r) => !r.posted).length;

		frm.set_intro(
			__("Transaction price {0} allocated across {1} obligations; {2} recognized, {3} plan rows unposted.", [
				fmt(frm.doc.transaction_price),
				frm.doc.obligations.length,
				fmt(recognized),
				pending,
			]),
			frm.doc.status === "Completed" ? "green" : "blue"
		);
	},

	post_recognition(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Post Recognition"),
			fields: [
				{
					fieldname: "until_date",
					fieldtype: "Date",
					label: __("Post Periods Ending On or Before"),
					default: frappe.datetime.get_today(),
					reqd: 1,
				},
			],
			primary_action_label: __("Post"),
			primary_action(values) {
				dialog.hide();
				frm.call({
					doc: frm.doc,
					method: "post_recognition",
					args: { until_date: values.until_date },
					freeze: true,
					freeze_message: __("Posting revenue recognition journal entries..."),
				}).then((r) => {
					if (r.exc || !r.message) return;
					const res = r.message;
					let message = __("{0} journal entries posted.", [res.posted]);
					if (res.journal_entries.length) {
						message += "<br><br>" + res.journal_entries.join("<br>");
					}
					if (res.pending_event) {
						message +=
							"<br><br>" +
							__("{0} rows are waiting for their obligation to be marked satisfied.", [
								res.pending_event,
							]);
					}
					frappe.msgprint({
						title: __("Revenue Recognition"),
						indicator: res.posted ? "green" : "orange",
						message: message,
					});
					frm.reload_doc();
				});
			},
		});
		dialog.show();
	},

	mark_obligation_satisfied(frm) {
		const unsatisfied = (frm.doc.obligations || []).filter(
			(o) => o.satisfaction_method === "Point in Time" && !o.satisfied
		);
		if (!unsatisfied.length) {
			frappe.msgprint(__("All Point in Time obligations are already satisfied."));
			return;
		}

		const options = unsatisfied.map((o) => ({
			value: String(o.idx),
			label: __("Row {0}: {1}", [o.idx, o.description]),
		}));

		const dialog = new frappe.ui.Dialog({
			title: __("Mark Obligation Satisfied"),
			fields: [
				{
					fieldname: "obligation_idx",
					fieldtype: "Select",
					label: __("Obligation"),
					options: options,
					reqd: 1,
				},
				{
					fieldname: "satisfied_date",
					fieldtype: "Date",
					label: __("Satisfied Date"),
					default: frappe.datetime.get_today(),
					reqd: 1,
				},
			],
			primary_action_label: __("Mark Satisfied"),
			primary_action(values) {
				dialog.hide();
				frm.call({
					doc: frm.doc,
					method: "mark_obligation_satisfied",
					args: {
						obligation_idx: values.obligation_idx,
						satisfied_date: values.satisfied_date,
					},
					freeze: true,
				}).then(() => frm.reload_doc());
			},
		});
		dialog.show();
	},
});
