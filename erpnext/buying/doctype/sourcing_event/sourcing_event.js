// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sourcing Event", {
	setup(frm) {
		frm.set_query("supplier", "suppliers", () => ({ filters: { disabled: 0 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		if (["Open"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Close Event"), () => close_event(frm));
		}

		if (["Closed", "Awarded"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Award Analysis"), () => show_award_analysis(frm), __("Award"));
			frm.add_custom_button(__("Create Supplier Quotation"), () => award_dialog(frm), __("Award"));
			frm.add_custom_button(__("Award Analysis Report"), () => {
				frappe.set_route("query-report", "Sourcing Event Award Analysis", {
					sourcing_event: frm.doc.name,
				});
			});
		}

		frm.add_custom_button(__("Bids"), () => {
			frappe.route_options = { sourcing_event: frm.doc.name };
			frappe.set_route("List", "Sourcing Event Bid");
		});

		if (frm.doc.sealed_until_close && !["Closed", "Awarded"].includes(frm.doc.status)) {
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline_alert(__("Bids are sealed until this event is closed."), "orange");
		}
	},
});

function close_event(frm) {
	const today = frappe.datetime.get_today();
	const early = frm.doc.close_date > today;

	frappe.confirm(
		early
			? __("This event closes on {0}. Force close it now?", [
					frappe.format(frm.doc.close_date, { fieldtype: "Date" }),
				])
			: __("Close this event? No further bids will be accepted."),
		() => {
			frm.call({
				doc: frm.doc,
				method: "close_event",
				args: { force: early ? 1 : 0 },
				freeze: true,
				freeze_message: __("Closing event..."),
			}).then((r) => {
				if (r.exc) return;
				frm.reload_doc();
			});
		},
	);
}

function show_award_analysis(frm) {
	frm.call({
		doc: frm.doc,
		method: "compute_award_analysis",
		freeze: true,
		freeze_message: __("Scoring bids..."),
	}).then((r) => {
		if (r.exc || !r.message) return;
		const res = r.message;
		const currency = res.currency || frappe.defaults.get_default("currency");

		const score_rows = (res.scores || [])
			.map(
				(row) => `<tr><td>${row.rank}</td><td>${frappe.utils.escape_html(row.supplier)}</td>
				<td class="text-right">${row.price_score}</td>
				<td class="text-right">${row.lead_time_score}</td>
				<td class="text-right">${row.scorecard_score}</td>
				<td class="text-right">${row.qualification_score}</td>
				<td class="text-right"><b>${row.weighted_total}</b></td></tr>`,
			)
			.join("");

		const scenario_rows = (res.scenarios || [])
			.map(
				(row) => `<tr><td>${__(row.name)}</td>
				<td class="text-right">${format_currency(row.total_cost, currency)}</td>
				<td>${row.awards.length}</td>
				<td>${row.coverage_gaps.length ? row.coverage_gaps.join(", ") : "-"}</td></tr>`,
			)
			.join("");

		const excluded = (res.excluded_suppliers || []).length
			? `<p class="text-danger">${__("Excluded (no valid qualification): {0}", [
					res.excluded_suppliers.join(", "),
				])}</p>`
			: "";

		frappe.msgprint({
			title: __("Award Analysis"),
			wide: true,
			message: `${excluded}
			<h5>${__("Supplier Ranking")}</h5>
			<table class="table table-bordered"><thead><tr>
			<th>${__("Rank")}</th><th>${__("Supplier")}</th><th class="text-right">${__("Price")}</th>
			<th class="text-right">${__("Lead Time")}</th><th class="text-right">${__("Scorecard")}</th>
			<th class="text-right">${__("Qualification")}</th><th class="text-right">${__("Weighted")}</th>
			</tr></thead><tbody>${score_rows}</tbody></table>
			<h5>${__("Award Scenarios")}</h5>
			<table class="table table-bordered"><thead><tr>
			<th>${__("Scenario")}</th><th class="text-right">${__("Total Cost")}</th>
			<th>${__("Lines")}</th><th>${__("Coverage Gaps")}</th>
			</tr></thead><tbody>${scenario_rows}</tbody></table>`,
		});
	});
}

function award_dialog(frm) {
	frm.call({ doc: frm.doc, method: "compute_award_analysis", freeze: true }).then((r) => {
		if (r.exc || !r.message) return;
		const suppliers = (r.message.scores || []).map((row) => row.supplier);
		if (!suppliers.length) {
			frappe.msgprint(__("No submitted bids to award."));
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Award to Supplier"),
			fields: [
				{
					fieldname: "supplier",
					fieldtype: "Select",
					label: __("Supplier"),
					options: suppliers,
					reqd: 1,
					default: suppliers[0],
				},
				{
					fieldname: "item_codes",
					fieldtype: "MultiSelectList",
					label: __("Items (leave blank for all)"),
					get_data() {
						return (frm.doc.items || []).map((row) => ({
							value: row.item_code,
							description: row.item_name || "",
						}));
					},
				},
			],
			primary_action_label: __("Create Draft Supplier Quotation"),
			primary_action(values) {
				frm.call({
					doc: frm.doc,
					method: "award_to",
					args: { supplier: values.supplier, item_codes: values.item_codes || [] },
					freeze: true,
					freeze_message: __("Creating Supplier Quotation..."),
				}).then((res) => {
					dialog.hide();
					if (res.exc) return;
					frm.reload_doc();
				});
			},
		});
		dialog.show();
	});
}
