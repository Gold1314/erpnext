// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Supplier Qualification", {
	setup(frm) {
		frm.set_query("template", () => ({ filters: { enabled: 1 } }));
		frm.set_query("supplier", () => ({ filters: { disabled: 0 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0 && frm.doc.template) {
			frm.add_custom_button(__("Load Template"), () => {
				frm.call({
					doc: frm.doc,
					method: "load_template",
					freeze: true,
					freeze_message: __("Loading questionnaire..."),
				}).then((r) => {
					if (r.exc || !r.message) return;
					frm.refresh_field("answers");
					frm.refresh_field("documents");
					frappe.show_alert({
						message: __("{0} questions and {1} documents loaded.", [
							r.message.questions,
							r.message.documents,
						]),
						indicator: "green",
					});
				});
			});
		}

		if (!frm.is_new()) {
			frm.add_custom_button(__("Document Status"), () => {
				frm.call({ doc: frm.doc, method: "get_document_summary" }).then((r) => {
					if (r.exc || !r.message) return;
					const rows = (r.message.statuses || [])
						.map((row) => {
							const days =
								row.days_to_expiry === null || row.days_to_expiry === undefined
									? "-"
									: row.days_to_expiry;
							return `<tr><td>${frappe.utils.escape_html(row.document_type || "")}</td>
								<td>${__(row.status)}</td><td class="text-right">${days}</td>
								<td>${row.is_mandatory ? __("Yes") : __("No")}</td></tr>`;
						})
						.join("");
					frappe.msgprint({
						title: __("Document Status"),
						indicator: r.message.mandatory_ok ? "green" : "red",
						message: `<p>${__("Mandatory documents OK: {0} · Expiring soon: {1}", [
							r.message.mandatory_ok ? __("Yes") : __("No"),
							r.message.expiring,
						])}</p>
						<table class="table table-bordered"><thead><tr>
						<th>${__("Document")}</th><th>${__("Status")}</th>
						<th class="text-right">${__("Days to Expiry")}</th><th>${__("Mandatory")}</th>
						</tr></thead><tbody>${rows}</tbody></table>`,
					});
				});
			});
		}

		set_status_indicator(frm);
	},

	template(frm) {
		if (frm.doc.template && !(frm.doc.answers || []).length) {
			frm.trigger("refresh");
		}
	},
});

frappe.ui.form.on("Supplier Qualification Answer", {
	score(frm) {
		frm.dirty();
	},
	passed(frm) {
		frm.dirty();
	},
});

function set_status_indicator(frm) {
	if (frm.is_new() || !frm.doc.risk_tier) return;
	const colors = { Low: "green", Medium: "orange", High: "red" };
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(
		__("Score {0}% · Risk tier {1}{2}", [
			frappe.format(frm.doc.score_percent, { fieldtype: "Percent" }),
			__(frm.doc.risk_tier),
			frm.doc.knockout_failures ? " · " + __("Knockout failures present") : "",
		]),
		frm.doc.knockout_failures ? "red" : colors[frm.doc.risk_tier] || "blue",
	);
}
