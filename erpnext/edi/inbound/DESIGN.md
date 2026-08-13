# Inbound E-Invoice Ingestion (supplier UBL → draft Purchase Invoice)

The reverse direction of `erpnext/edi/ubl/` (Sales Invoice → UBL XML). This
closes the AP invoice-capture gap in procure-to-pay **without OCR**: a
structured e-invoice already carries the item codes, order references, VAT
breakdown and totals that OCR can only guess at, and in a growing number of
countries it is the legally mandated exchange format anyway.

## Architecture

Same pure-core + adapter doctrine as the outbound side, mirrored:

```
                 pure core (no frappe)                    frappe adapters
┌──────────────────────────────────────────────┐   ┌───────────────────────────┐
│ models.py    ParsedInvoice dataclasses        │   │ mapper.py  parsed →       │
│              + validate() (EN 16931 checks)   │◄──┤            staging payload│
│ parser.py    UBL 2.1 XML → ParsedInvoice      │   │            (resolution)   │
│              (xml.etree, URI-based lookup)    │   │ api.py     whitelisted    │
│ matcher.py   invoice lines ↔ PO lines         │   │            endpoints      │
│              (difflib, deterministic)         │   │ doctypes   staging + child│
└──────────────────────────────────────────────┘   └───────────────────────────┘
```

| Outbound (`edi/ubl`) | Inbound (`edi/inbound`) |
|---|---|
| `models.CanonicalInvoice` | `models.ParsedInvoice` |
| `builder.build_invoice_xml` | `parser.parse_ubl_invoice` |
| `profiles.Profile` (CIUS we emit) | `raw_profile` recorded verbatim (we accept anything) |
| `mapper.sales_invoice_to_canonical` | `mapper.parsed_to_inbound_doc` |
| `api.generate_e_invoice` | `api.import_einvoice_xml` |
| `EDI Transmission Log` | `Inbound E-Invoice` (staging) |

`models.py`, `parser.py` and `matcher.py` import **no frappe** and are unit
tested standalone: `python erpnext/edi/inbound/test_inbound.py` (53 tests),
using the same file-path bootstrap as `erpnext/edi/ubl/test_ubl.py`.

## The round-trip test

`TestRoundTrip` builds XML with the **outbound builder** and reads it back
with the **inbound parser**, asserting every header field, both parties,
every line, every tax subtotal and every total survives — including the
escaped `&`/`<`/`>` characters and a two-hop `parse → rebuild → parse` fixed
point. The two sides were written independently against the UBL spec, so
their agreement is real evidence rather than a restatement of one
implementation, and it is the cheapest regression net available: any future
change to either side that breaks the contract fails here without a site.

## Namespace resolution (the #1 interop bug)

Senders bind the UBL namespaces to whatever prefixes their toolchain likes —
`cbc:`, `ns2:`, `q1:`, `x:`, or *no prefix at all* when one of the three is
declared as the default namespace. A prefix is a serialization detail; only
the **namespace URI** identifies an element.

Every lookup in `parser.py` goes through `_find`/`_findtext`, which take a
path written with *our* conventional prefixes, resolve those prefixes
through the `NS` URI table, and compare `{namespace-uri}local-name` against
what ElementTree produced. Consequences:

- `<cbc:ID>`, `<ns0:ID>` and a default-namespaced `<ID>` all parse identically
  (tested: `test_same_document_reserialized_with_other_prefixes_parses_identically`).
- An element with the right local name in a *foreign* namespace is ignored
  (tested: `test_foreign_namespace_with_the_same_local_name_is_ignored`) —
  a vendor extension cannot hijack `cbc:ID`.
- Documents with no namespace declarations at all (some ERP exports) are
  tolerated as a fallback.

Nothing in the parser ever string-matches a prefix.

## Tolerance and error policy

| Situation | Behaviour |
|---|---|
| Not well-formed XML / not an `Invoice` root / `CreditNote` root | `ParseError` → `frappe.throw` with a specific message |
| Non-numeric amount | `ParseError` naming the element |
| Missing optional element | `None` (or the type's zero) — never a guess |
| No invoice number or no lines | refused: there is nothing to stage |
| `validate()` errors (totals mismatch, missing seller VAT, …) | **staged anyway** in status *Exception* with the errors listed |
| Unresolvable supplier / item / account | field left blank + a warning; the operator completes it |

Dropping a received invoice on the floor is worse than showing an accountant
a bad one — an invoice that arrived is a liability whether or not it parses
cleanly. Only payloads that are not usable invoices at all are refused.

## Resolution, not creation

`erpnext/regional/doctype/import_supplier_invoice` (Italy-only, FatturaPA)
creates Suppliers, Contacts, Addresses and UOMs on the fly from file
contents. This mapper deliberately does **not**. A supplier e-invoice is
untrusted third-party input; minting master data from it is how duplicate
supplier records and unusable item masters get into a ledger. Every failed
lookup leaves the field blank, warns, and stays editable — fixed once by a
human, and the next invoice from that supplier resolves automatically.

| Concern | Resolution order | Warns |
|---|---|---|
| Supplier | `Supplier.tax_id` = seller VAT → exact `supplier_name` → exact record name → a *single* `like` hit | on everything but the VAT hit; several `like` hits resolve to nothing on purpose |
| Item | `BuyersItemIdentification` (BT-156) vs `Item.name` → `SellersItemIdentification` (BT-155) vs `Item Supplier.supplier_part_no` scoped to the supplier | when unresolved; descriptions are **never** used to guess an item |
| UOM | `Common Code` (reverse of the outbound UN/ECE Rec 20 mapping) → builtin reverse of `edi.ubl.mapper.UOM_CODE_FALLBACK` → a UOM named like the code → `Stock Settings.stock_uom` | on the last two |
| Expense account | `Item Default.expense_account` for the company → `Company.default_expense_account` | on the company fallback |
| Tax account | default `Purchase Taxes and Charges Template` → any template of the company → the company's only `account_type = "Tax"` account | on every path |
| Currency | as sent, checked against `Currency`; absent → company currency | when absent, unknown, or ≠ company currency |

## Matching

`matcher.match_lines(parsed_lines, po_lines, tolerance_pct)`, strongest
evidence first, each pass running across *all* lines before the next begins,
so weak evidence never claims a PO line that strong evidence wanted:

1. `item_code` — buyer item code equals the PO line's item code.
2. `order_line_ref` — `OrderLineReference/LineID` (BT-132) equals the PO
   line's `idx`.
3. `description` — normalized `SequenceMatcher` ratio ≥ **0.85**.
4. `unmatched`.

Each PO line is claimed at most once; ties between equally-good fuzzy
candidates break on PO line position. The whole function is deterministic
(tested over repeated runs).

Variances are signed percentages against the PO line: price against `rate`,
quantity against ordered `qty`. A zero baseline reports 100% rather than
dividing by zero. `summarize_match` additionally flags **over-billing**
against `remaining_qty` — a line can be inside the quantity tolerance versus
the order and still exceed what is left unbilled.

`remaining_qty` is derived as `qty × (amount − billed_amt) / amount`:
`Purchase Order Item` tracks billing in money, not quantity, so the open
quantity is the un-billed fraction of the ordered quantity.

Once matched, `purchase_order`/`po_detail` are written onto the Purchase
Invoice rows and **ERPNext's own three-way matching takes over** — this
module deliberately stops at finding the links.

## Safety rules

**Never submit.** `create_purchase_invoice` inserts a *draft* (`docstatus 0`)
and stops, then routes the user to it. A document a third party generated
must be approved by a human before it hits the general ledger; auto-submit
would let anyone who knows our inbox address post entries to our books.

**Never double-book.** `create_purchase_invoice` refuses when a draft or
submitted Purchase Invoice already carries the same `supplier` + `bill_no`,
naming the offending document and telling the user to cancel or amend it.
Import additionally *warns* (does not block) when the same invoice number was
staged before, or already exists on a Purchase Invoice — resends are normal,
a second payable is not.

**Gated creation.** A Purchase Invoice can only be created from status
*Pending Review* or *Matched*, with a supplier, a currency, an `item_code` on
every line and an `account_head` on every tax row. *Exception* documents must
be fixed (and re-matched) first; `Rejected` and `Invoice Created` are
terminal.

**Line total wins over unit price.** When `unit_price × qty` disagrees with
the supplier's `LineExtensionAmount` beyond 0.02 (a line discount, a rounding
convention we do not model), the rate is set to `line_net / qty` and a
warning is recorded. An AP document that does not add up to the supplier's
own total is worse than one with an adjusted unit price.

## Verified field mapping

Every fieldname below was checked against the doctype JSON in this repo.

### Written to `Purchase Invoice`

| Field | Type (verified) | Source |
|---|---|---|
| `company` | Link → Company | the `company` argument (never the buyer party in the file) |
| `supplier` | Link → Supplier | resolved from seller VAT / name |
| `currency` | Link → Currency | `DocumentCurrencyCode` |
| `conversion_rate` | Float | `1.0` when the invoice currency is the company currency; otherwise left to `set_missing_values` |
| `posting_date` | Date | `today()` |
| `bill_no` | Data | `cbc:ID` (BT-1) |
| `bill_date` | Date | `cbc:IssueDate` (BT-2) |
| `due_date` | Date | `cbc:DueDate` (BT-9) |
| `items` | Table → Purchase Invoice Item | below |
| `taxes` | Table → Purchase Taxes and Charges | below |
| `credit_to` | Link → Account | **not set** — left to `set_missing_values`/party defaults, which resolve the company's payable account |

### Written to `Purchase Invoice Item`

| Field | Type (verified) | Source |
|---|---|---|
| `item_code` | Link → Item | resolved item (mandatory before creation) |
| `item_name` | Data (reqd) | parsed `Item/Name`, truncated to 140 |
| `description` | Text Editor | parsed `Item/Description` |
| `qty` | Float (reqd) | `InvoicedQuantity` |
| `uom` | Link → UOM (reqd) | resolved from `unitCode` |
| `conversion_factor` | Float (reqd) | `set_missing_values`, defaulted to 1.0 if still empty |
| `rate` | Currency (reqd) | `Price/PriceAmount`, or `line_net / qty` when they disagree |
| `amount` | Currency (reqd) | **not set** — ERPNext computes `rate × qty` |
| `expense_account` | Link → Account | Item Default → Company default |
| `purchase_order` | Link → Purchase Order | `matched_po` |
| `po_detail` | Data | `matched_po_detail` (Purchase Order Item row name) |
| `purchase_receipt` / `pr_detail` | Link → Purchase Receipt / Data | **not set** — receipt matching is ERPNext's job once `po_detail` is present |

### Written to `Purchase Taxes and Charges`

| Field | Type (verified) | Value |
|---|---|---|
| `charge_type` | Select (reqd) | `"Actual"` — book exactly what the supplier charged |
| `account_head` | Link → Account (reqd) | resolved tax account (mandatory before creation) |
| `tax_amount` | Currency | `TaxSubtotal/TaxAmount` |
| `description` | Small Text (reqd) | `VAT {rate}% ({category})` — ERPNext nulls `rate` for `Actual` rows (`accounts/services/taxes.validate_taxes_and_charges`), so the percentage is preserved here |
| `category` | Select (reqd, default `Total`) | `"Total"` |
| `add_deduct_tax` | Select (reqd, default `Add`) | `"Add"` |
| `rate` | Float | **not set** — nulled by ERPNext for `Actual` |

### Read from other doctypes

| Doctype | Fields read (verified) |
|---|---|
| `Purchase Order` | `name`, `supplier`, `company`, `status`, `per_billed`, `transaction_date`, `docstatus` |
| `Purchase Order Item` | `name`, `parent`, `idx`, `item_code`, `description`, `qty`, `rate`, `amount`, `billed_amt`, `received_qty` |
| `Supplier` | `name`, `tax_id`, `supplier_name`, `disabled` |
| `Item` | `name` (= `item_code`, `autoname: field:item_code`) |
| `Item Supplier` | `supplier`, `supplier_part_no`, `parent`, `parenttype` |
| `Item Default` | `parent`, `company`, `expense_account` |
| `Company` | `default_currency`, `default_expense_account` |
| `Account` | `company`, `account_type`, `is_group`, `disabled` |
| `Purchase Taxes and Charges Template` | `name`, `company`, `is_default`, `disabled` |
| `Common Code` / `Dynamic Link` | `common_code`, `applies_to` → `link_doctype`, `link_name` |
| `Stock Settings` | `stock_uom` |

## Naming note

Frappe resolves a doctype's controller path with `frappe.scrub(name)`, which
turns both spaces *and hyphens* into underscores. So **Inbound E-Invoice**
lives in `erpnext/edi/doctype/inbound_e_invoice/`, its children in
`inbound_e_invoice_item` / `inbound_e_invoice_tax`, and the report **Inbound
E-Invoice Queue** in `erpnext/edi/report/inbound_e_invoice_queue/`. No
doctype in erpnext deviates from this rule (verified across every doctype
JSON in the repo); a folder named `inbound_einvoice` would simply fail to
load.

## Recommended wiring (NOT applied — outside the file scope)

### 1. E-mail inbox auto-import

Most inbound e-invoices arrive as an attachment on a mailbox. Add a hook on
`Communication` (or a dedicated `Email Account` for AP) that feeds XML
attachments straight into `import_einvoice_xml`:

```python
# hooks.py
doc_events = {
    "File": {
        "after_insert": "erpnext.edi.inbound.email_inbox.auto_import_einvoice",
    },
}
```

```python
# erpnext/edi/inbound/email_inbox.py  (sketch)
def auto_import_einvoice(doc, method=None):
    if doc.attached_to_doctype != "Communication" or not doc.file_name.lower().endswith(".xml"):
        return
    account = frappe.db.get_value("Communication", doc.attached_to_name, "email_account")
    company = frappe.db.get_value("Email Account", account, "company")  # custom field
    if not company:
        return
    frappe.enqueue(
        "erpnext.edi.inbound.api.import_einvoice_xml",
        queue="short",
        company=company,
        xml_content=doc.get_content().decode("utf-8-sig"),
        filename=doc.file_name,
    )
```

Guard rails to add with it: only accept XML under a size cap, catch
`ParseError`/`ValidationError` into an Error Log rather than failing the
mail sync, and de-duplicate on `(sender, invoice_id)` — the duplicate
warnings already computed by `_duplicate_warnings` cover the reporting side.

### 2. Purchase Invoice form link

Show the source document from the invoice it produced:

```js
// erpnext/public/js/purchase_invoice_einvoice.js
frappe.ui.form.on("Purchase Invoice", {
    refresh(frm) {
        if (frm.doc.docstatus > 1 || !frm.doc.bill_no) return;
        frappe.db.get_value("Inbound E-Invoice", { purchase_invoice: frm.doc.name }, "name")
            .then((r) => {
                if (!r.message || !r.message.name) return;
                frm.add_custom_button(__("Source E-Invoice"), () =>
                    frappe.set_route("Form", "Inbound E-Invoice", r.message.name));
            });
    },
});
```

Cleaner alternative once a field can be added: a read-only `Link` field
`inbound_einvoice` on Purchase Invoice plus a `links` entry on the staging
doctype, giving a real dashboard connection instead of a lookup.

### 3. Scheduler

```python
# hooks.py
scheduler_events = {
    "hourly_long": [
        "erpnext.edi.inbound.api.match_pending_einvoices",   # re-match Pending Review docs
    ],
    "daily": [
        "erpnext.edi.inbound.api.notify_stale_einvoices",    # ageing > N days → AP manager
    ],
}
```

Re-matching on a schedule matters because the *PO* often arrives after the
invoice (or is amended); an invoice that could not match yesterday may match
today without anyone touching it. The ageing column and the "Oldest Open"
summary card on the queue report are the signal the notifier should use.

### 4. Access-point receive

`erpnext/edi/ubl/DESIGN.md` sketches `EDIProviderAdapter.receive()` as the
extension point for pulling documents off a Peppol access point. It should
call `import_einvoice_xml` per document — the whole ingestion path above is
already transport-agnostic.

## Testing

`python erpnext/edi/inbound/test_inbound.py` — 53 pure tests:

- **round trip** against the outbound builder (header, parties, lines, taxes,
  totals, escaped characters, two-hop fixed point, absent cross-references);
- **namespace resolution** with `ns0:`/`q1:`/default-namespaced documents,
  prefix-swapped re-serialization, foreign-namespace hijack attempts,
  namespace-free documents;
- **tolerance** of missing optional elements, BOM/bytes input, `PayableAmount`
  and `LineExtensionAmount` fallbacks;
- **ParseError** on garbage, empty payload, wrong root, `CreditNote`,
  non-numeric amounts;
- **validation** of totals mismatches (both sides), the 0.02 boundary,
  missing seller VAT / invoice ID / dates / currency;
- **matcher**: all four strategies, pass ordering, single-claim,
  determinism over repeated runs, tie-breaking, price/quantity variance
  inside and outside tolerance, over-billing against remaining quantity,
  zero-baseline division;
- **end-to-end on the pure side**: builder → parser → matcher, including a
  supplier price increase caught as a +15.00% exception.

`mapper.py`, `api.py` and the doctypes need a site and are exercised in
integration testing (they require Supplier/Item/Purchase Order fixtures).
