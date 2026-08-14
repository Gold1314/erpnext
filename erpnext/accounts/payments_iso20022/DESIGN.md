# ISO 20022 pain.001 Bank Payment File Generation

Closes the last step of procure-to-pay: ERPNext could already *batch* approved
payments into a `Payment Order`, but produced no file, so nothing could
actually be paid without re-keying every line into a bank portal. This module
turns a submitted Payment Order into a bank-uploadable
`pain.001` CustomerCreditTransferInitiation file.

It follows the repo-wide **pure-core + adapter** doctrine, mirroring
`erpnext/edi/ubl/`.

## Architecture

```
                 pure core (no frappe)                    frappe adapters
┌──────────────────────────────────────────────┐   ┌──────────────────────────┐
│ models.py    PaymentParty / PaymentTransaction│   │ mapper.py  Payment Order │
│              / PaymentBatch + validate()      │◄──┤            → PaymentBatch│
│              + is_valid_iban (ISO 7064 MOD-97)│   │ api.py     whitelisted   │
│ builder.py   PaymentBatch → pain.001 XML      │   │            endpoints     │
│              (xml.etree, deterministic,       │   │ doctypes   Profile / Log │
│               v03 + v09)                      │   │ report     File Status   │
└──────────────────────────────────────────────┘   └──────────────────────────┘
```

- `models.py` and `builder.py` import **no frappe** and are unit-tested
  standalone: `python erpnext/accounts/payments_iso20022/test_iso20022.py`
  (49 tests; the file-path bootstrap mirrors `erpnext/edi/ubl/test_ubl.py`).
- `mapper.py` is the single place that knows Payment Order fieldnames.
- `api.py` orchestrates: resolve profile → map → validate → build → attach
  File → create `Bank Payment File Log`.

## Determinism

Nothing in the pure core reads the clock. `PaymentBatch.creation_date_time` is
**a constructor argument**; `api.py` supplies `frappe.utils.now_datetime()`.
`MsgId` is likewise derived from the Payment Order name plus a generation
counter (`frappe.db.count` of existing logs + 1), never a timestamp. The result
is that a given batch always serializes byte-identically — the property the
determinism tests assert, and what makes a regenerated file diffable against
the one the bank received.

The builder also avoids `ET.register_namespace`: registering the prefix `""`
for a second URI *deletes* the first registration, so only one of the two
variants could ever be the default namespace. Instead the namespace is written
as a literal `xmlns` attribute on the root `Document` element and every child
uses a bare local name, inheriting it — the exact shape of every bank's sample
file, with no global state and no cross-thread interference.

## pain.001.001.03 vs pain.001.001.09

Both are supported because banks are split between them: `.03` is the version
the original SEPA rulebooks standardized on and many European portals still
require, `.09` is the current ISO maintenance release that newer portals
mandate. Picking the wrong one is a hard rejection, so it is a per-profile
setting, not a global constant.

| Concern | `pain.001.001.03` | `pain.001.001.09` |
|---|---|---|
| Namespace | `urn:iso:std:iso:20022:tech:xsd:pain.001.001.03` | `…:pain.001.001.09` |
| Requested execution date | `<ReqdExctnDt>2026-09-01</ReqdExctnDt>` (plain `ISODate`) | `<ReqdExctnDt><Dt>2026-09-01</Dt></ReqdExctnDt>` (a `Dt`/`DtTm` choice) |
| Agent BIC element | `FinInstnId/BIC` | `FinInstnId/BICFI` |

The BIC rename applies to **both** `DbtrAgt` and `CdtrAgt`; all four
combinations are asserted in `TestVariantDifferences`. Everything else this
builder emits is common to the two schemas, including the relative element
order inside `GrpHdr`, `PmtInf` and `CdtTrfTxInf`.

## Message structure

One Payment Order maps to exactly one `PmtInf` block, because a Payment Order
debits one company bank account (`company_bank_account`) on one execution date
— precisely the grouping key ISO 20022 defines for `PmtInf`. Mixed execution
dates would be silently collapsed into one, so `validate()` rejects them.

```
Document
└── CstmrCdtTrfInitn
    ├── GrpHdr           MsgId, CreDtTm, NbOfTxs, CtrlSum, InitgPty/Nm
    └── PmtInf           PmtInfId, PmtMtd, BtchBookg, NbOfTxs, CtrlSum,
                         [PmtTpInf/SvcLvl/Cd], ReqdExctnDt,
                         Dbtr/Nm[+PstlAdr], DbtrAcct/Id/IBAN,
                         [DbtrAgt/FinInstnId/BIC|BICFI], ChrgBr
        └── CdtTrfTxInf  PmtId/EndToEndId, Amt/InstdAmt[@Ccy],
                         [CdtrAgt/FinInstnId/BIC|BICFI], Cdtr/Nm,
                         CdtrAcct/Id/IBAN, [Purp/Cd], [RmtInf/Ustrd]
```

`SvcLvl/Cd = SEPA` is emitted only when **every** transaction is in EUR — SEPA
is a euro-only service level and claiming it on a mixed-currency file is a
guaranteed rejection. Setting the profile's service level to `None` omits
`PmtTpInf` entirely (for non-SEPA/domestic schemes).

Amounts are always `"%.2f"`. `RmtInf/Ustrd` is truncated to 140 characters
(`Max140Text`); `MsgId`, `PmtInfId` and `EndToEndId` to 35 (`Max35Text`).

## Verified fieldnames

Every fieldname was read off the shipped JSON, not assumed.

| Doctype | Field | Used for |
|---|---|---|
| Payment Order | `company` | debtor name, profile resolution, report filter |
| Payment Order | `company_bank_account` (Link → Bank Account, reqd) | the debited account |
| Payment Order | `company_bank` (fetched from the account's `bank`) | not used — the Bank is read from the Bank Account itself |
| Payment Order | `payment_order_type` (`Payment Request` / `Payment Entry`) | currency + remittance strategy |
| Payment Order | `posting_date` | default execution date |
| Payment Order | `references` (Table → Payment Order Reference) | the transactions |
| Payment Order Reference | `reference_doctype`, `reference_name` | end-to-end ID, remittance |
| Payment Order Reference | `amount` (Currency, reqd) | `InstdAmt` |
| Payment Order Reference | `supplier` (Link → Supplier) | creditor fallback name |
| Payment Order Reference | `payment_request` (Link → Payment Request) | currency lookup |
| Payment Order Reference | `bank_account` (Link → Bank Account, reqd) | creditor account |
| Bank Account | `iban` | `IBAN` |
| Bank Account | `bank_account_no` | `Othr/Id` fallback |
| Bank Account | `bank` (Link → Bank) | BIC lookup |
| Bank Account | `account_name`, `party`, `party_type` | creditor name |
| Bank Account | `is_company_account`, `company`, `disabled` | profile validation |
| Bank | `swift_number` | `BIC` / `BICFI` |
| Supplier | `supplier_name` | creditor display name |
| Supplier | `default_bank_account` | creditor account fallback |
| Payment Request | `currency` | transaction currency |
| Payment Entry | `paid_from_account_currency` | transaction currency |
| Payment Entry Reference | `bill_no`, `reference_name` | remittance line |
| Purchase Invoice | `bill_no` | remittance line |

Note that `Payment Order Reference.bank_account` is populated by both
`make_payment_order` mappers — from `Payment Request.bank_account` and from
`Payment Entry.party_bank_account` — so it is always the *supplier's* account,
never the company's.

## Currency decision

A Payment Order has no currency field, and the child row's `amount` inherits
the currency of the document it came from:

| `payment_order_type` | Row `amount` comes from | Currency read from |
|---|---|---|
| `Payment Request` | `Payment Request.grand_total` | `Payment Request.currency` (via the row's `payment_request`) |
| `Payment Entry` | `Payment Entry.paid_amount` | `Payment Entry.paid_from_account_currency` |
| neither resolves | — | `Company.default_currency`, **with a warning** |

The company default is deliberately the last resort: a mis-stated currency in
a payment file moves real money at the wrong amount.

## Fallback and warning design

Every fallback appends a human-readable string to the `warnings` list, which
`api.py` returns and persists on the log's `warnings` field. Warnings never
block generation; **errors** (from `validate()`) always do.

| Situation | Behaviour | Severity |
|---|---|---|
| Row has no `bank_account` | fall back to `Supplier.default_bank_account` | warning |
| Neither exists | creditor is the bare supplier name, no IBAN | warning **and** a blocking `validate()` error (`IBAN is missing`) |
| Bank Account has no `iban` | party built without an IBAN | warning **and** a blocking error |
| Bank has no `swift_number` | `CdtrAgt`/`DbtrAgt` block is omitted entirely | warning (BIC is optional in SEPA "IBAN only") |
| Bank Account disabled but referenced | still used | warning |
| Currency unresolvable | company default currency | warning |
| No invoice reference derivable | `RmtInf` omitted | warning |
| Remittance line > 140 chars | truncated by the builder | warning |
| Duplicate `reference_name` across rows | end-to-end ID suffixed `-2`, `-3`, … | warning |
| No execution date passed | Payment Order `posting_date` | warning |

The design principle: a missing IBAN produces a **warning that names the
supplier and the row** *and* a blocking validation error, so the user sees both
"which supplier is missing bank details" and "the file was not produced".

`validate()` blocks on: missing/invalid IBAN (real MOD-97 checksum, not a
length test), malformed BIC, non-positive amounts, non-ISO-4217 currency,
control-sum mismatch, transaction-count mismatch, duplicate or over-long
end-to-end IDs, over-long or missing `MsgId`, missing execution dates, mixed
execution dates, unsupported charge bearer, and an empty batch.

## End-to-end IDs

`EndToEndId` is the row's `reference_name` (the Payment Entry, or the invoice
behind a Payment Request), sanitized to the SEPA-safe character set
(`a-z A-Z 0-9 / - ? : ( ) . , ' +` and space) and truncated to 35 characters.
Collisions — two rows paying the same document, e.g. a split payment — get a
numeric suffix that eats into the identifier from the right so the 35-character
cap still holds. Banks reject duplicate end-to-end references within one
message, and this reference is what comes back on the account statement, so it
is the reconciliation key.

## Transmission lifecycle

```
generate_payment_file ──► Generated ──► Transmitted
                              │  mark_transmitted()
                              └───────► Failed  (log.mark_failed)
```

v1 transmission is **manual on purpose**. The file is attached to the Payment
Order as a private File; the treasurer uploads it to the bank portal and
confirms via `mark_transmitted`, which stamps `transmitted_on`. The confirm
dialog shows the transaction count and control sum so they can be checked
against what the portal reports — that comparison is the whole reason ISO 20022
carries `NbOfTxs`/`CtrlSum`.

Direct connectivity (EBICS, host-to-host SFTP, bank REST APIs) is a per-bank
integration with its own credentials and signing requirements. It belongs
behind an adapter selected on `Bank Payment Profile`, in the same shape as
`EDI Transmission Profile.transport_mode` in the UBL module.

Each generation creates a **new** log row rather than updating the previous
one: each file carries its own `MsgId` and may already sit in a bank portal, so
the history of what was produced must stay intact for audit. The
`Payment File Status` report joins each Payment Order to its *latest* log.

## Recommended Payment Order form buttons (NOT applied)

`erpnext/accounts/doctype/payment_order/payment_order.js` is outside this
module's file scope. To wire the feature into the form, add this to the
existing `refresh` handler:

```js
	refresh: function (frm) {
		// ... existing handlers ...

		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(
				__("Generate Bank File"),
				function () {
					frm.trigger("generate_bank_payment_file");
				},
				__("Bank Payment File")
			);

			frm.add_custom_button(
				__("Download Bank File"),
				function () {
					const execution_date = frm.doc.__iso20022_execution_date || "";
					open_url_post(
						"/api/method/erpnext.accounts.payments_iso20022.api.download_payment_file",
						{ payment_order: frm.doc.name, execution_date: execution_date }
					);
				},
				__("Bank Payment File")
			);
		}
	},

	generate_bank_payment_file: function (frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Generate Bank Payment File"),
			fields: [
				{
					fieldtype: "Link",
					fieldname: "profile",
					label: __("Bank Payment Profile"),
					options: "Bank Payment Profile",
					description: __("Leave empty to use the company default profile"),
					get_query: () => ({ filters: { company: frm.doc.company } }),
				},
				{
					fieldtype: "Date",
					fieldname: "execution_date",
					label: __("Requested Execution Date"),
					default: frm.doc.posting_date,
					reqd: 1,
				},
			],
			primary_action_label: __("Generate"),
			primary_action(values) {
				frappe.call({
					method: "erpnext.accounts.payments_iso20022.api.generate_payment_file",
					args: {
						payment_order: frm.doc.name,
						profile: values.profile,
						execution_date: values.execution_date,
					},
					freeze: true,
					freeze_message: __("Generating payment file..."),
					callback: function (r) {
						dialog.hide();
						if (!r.message) return;

						if (r.message.warnings && r.message.warnings.length) {
							frappe.msgprint({
								title: __("Generated with warnings"),
								indicator: "orange",
								message: r.message.warnings
									.map((w) => frappe.utils.escape_html(w))
									.join("<br>"),
							});
						}

						frappe.show_alert({
							message: __("Payment file {0} created", [
								`<a href="/app/bank-payment-file-log/${r.message.log}">${r.message.log}</a>`,
							]),
							indicator: "green",
						});

						window.open(r.message.file_url);
						frm.reload_doc();
					},
				});
			},
		});
		dialog.show();
	},
```

The `Bank Payment File Log` list can also be surfaced from the Payment Order
dashboard by adding it to `payment_order_dashboard.py`:

```python
	"transactions": [
		# ... existing groups ...
		{"label": _("Bank File"), "items": ["Bank Payment File Log"]},
	],
```

## Hooks

**None are required.** The doctypes, the report and the whitelisted endpoints
are all discovered by `bench migrate` from their module folders — nothing needs
registering in `hooks.py`. Specifically:

- no `doc_events` — generation is user-initiated, never a side effect of
  submitting a Payment Order (a file must not be produced before a treasurer
  has chosen the execution date);
- no `scheduler_events` — v1 has no automatic transmission to schedule;
- no `fixtures` — `Bank Payment Profile` records are customer-specific bank
  configuration, not shipped data.

If the module is later surfaced in the Accounts workspace, add
`Bank Payment Profile`, `Bank Payment File Log` and the `Payment File Status`
report as links in `erpnext/accounts/workspace/accounting/accounting.json`.

## Testing

`python erpnext/accounts/payments_iso20022/test_iso20022.py` runs the pure core
without a site or a database: 49 tests covering the MOD-97 IBAN validator
against published specimen IBANs and mutated ones, group-header and
per-transaction serialization, the v03/v09 differences, remittance truncation,
XML escaping, byte-for-byte determinism, and every `validate()` rule.
