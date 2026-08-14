// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Close Cycle", {
	refresh(frm) {
		if (frm.is_new()) return;

		frappe.db.count("Close Task", { filters: { close_cycle: frm.doc.name } }).then((count) => {
			if (!count) {
				if (frm.doc.close_task_template) {
					frm.add_custom_button(__("Create Tasks"), () => {
						frm.call({
							doc: frm.doc,
							method: "create_tasks",
							freeze: true,
							freeze_message: __("Creating close tasks..."),
						}).then(() => frm.reload_doc());
					});
				}
				return;
			}

			frm.add_custom_button(__("Run Auto Verifications"), () => {
				frappe.call({
					method: "erpnext.accounts.closing.verifications.run_auto_verifications",
					args: { close_cycle: frm.doc.name },
					freeze: true,
					freeze_message: __("Verifying close tasks against accounting records..."),
					callback(r) {
						if (r.exc || !r.message) return;
						const res = r.message;
						const details = (res.results || [])
							.map((row) => `${row.ok ? "✔" : "✘"} ${row.task_title}: ${row.message}`)
							.join("<br>");
						frappe.msgprint({
							title: __("Auto Verification"),
							indicator: res.failed ? "orange" : "green",
							message:
								__("{0} tasks checked, {1} completed, {2} failed.", [
									res.tasks_checked,
									res.completed,
									res.failed,
								]) + (details ? "<br><br>" + details : ""),
						});
						frm.reload_doc();
					},
				});
			});

			frm.add_custom_button(__("Open Tasks"), () => {
				frappe.route_options = { close_cycle: frm.doc.name };
				frappe.set_route("List", "Close Task");
			});
		});
	},
});
