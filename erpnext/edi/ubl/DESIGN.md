# UBL 2.1 / Peppol BIS 3.0 E-Invoicing Framework

Implements blueprint item **W3-3c** on the EDI module, following the
repo-wide **pure-core + adapter** doctrine (section 2 of
`GAP_CLOSURE_BLUEPRINT.md`).

## Architecture

```
                 pure core (no frappe)                    frappe adapters
┌──────────────────────────────────────────────┐   ┌──────────────────────────┐
│ models.py    canonical invoice dataclasses    │   │ mapper.py  SI → canonical│
│              + validate() (EN 16931 checks)   │◄──┤            + Common Code │
│ profiles.py  CIUS registry (CustomizationID/  │   │            lookups       │
│              ProfileID + per-CIUS checks)     │   │ api.py     whitelisted   │
│ builder.py   canonical → UBL 2.1 XML          │   │            endpoints     │
│              (xml.etree, deterministic)       │   │ doctypes   Profile / Log │
└──────────────────────────────────────────────┘   └──────────────────────────┘
```

- `models.py`, `profiles.py`, `builder.py` import **no frappe** and are
  unit-tested standalone: `python erpnext/edi/ubl/test_ubl.py` (the file-path
  bootstrap mirrors `erpnext/accounts/forecasting/test_engine.py`).
- `mapper.py` is the single place that knows Sales Invoice fieldnames.
- `api.py` orchestrates: resolve profile → map → validate → build → attach
  File → upsert `EDI Transmission Log`.

## Profiles (CIUS)

| Key | CustomizationID | Extra checks |
|---|---|---|
| `peppol-bis-3` | `urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0` | – |
| `xrechnung-3` | `urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0` | buyer reference (BT-10) and seller electronic address (BT-34) required |
| `en16931` | `urn:cen.eu:en16931:2017` | – |

All share ProfileID `urn:fdc:peppol.eu:2017:poacc:billing:01:1.0`. New
country profiles are one dict entry + optional check function; ship them as
part of per-country fixture packs.

## Currency decision

All amounts are in the **invoice (document) currency**: `items[].net_amount`,
`items[].net_rate`, header `net_total`, `total_taxes_and_charges`,
`grand_total`. The `base_*` (company currency) twins are never used — UBL's
`DocumentCurrencyCode` is the SI `currency` and every amount in the file must
be consistent with it. `PayableAmount` (BT-115) = `grand_total`
(not `outstanding_amount`, which is a balance, and not `rounded_total`).

## Code resolution & fallback design

Standardized codes resolve through the existing **Common Code** substrate
(genericode-imported UN/CEFACT lists, dynamic-link `applies_to` rows):

| Concern | Common Code lookup | Fallback | Warning emitted |
|---|---|---|---|
| UOM → UN/ECE Rec 20 | `link_doctype = "UOM"` | small builtin map (Nos/Unit→C62, Kg→KGM, Litre→LTR, Meter→MTR, Hour→HUR, Box→XBX, Set→SET), else `C62` | on default `C62` |
| Payment means (UNCL4461) | `link_doctype = "Mode of Payment"` (first payment-schedule row) | `30` credit transfer | on fallback |
| Country → ISO 3166-1 | builtin map of common names, then `Country.code` | – | when unresolvable |
| Tax category (UNCL5305) | rate > 0 → `S`; rate 0 → `Z` | – | on every `Z` (review E/AE/K) |

Every fallback appends a human-readable string to the `warnings` list, which
is returned by the API and persisted on the log's `warnings` field — the user
always sees which decisions the mapper took for them.

**Known v1 simplifications** (each produces a warning):
- Taxable base per rate is derived arithmetically (`tax_amount / rate`)
  because header tax rows carry no per-rate base. Keeps BR-CO-17 consistent.
- All lines are classified at the first non-zero header VAT rate; mixed-rate
  invoices warn to split or extend the mapper with itemised tax details.
- `charge_type = "Actual"` tax rows are excluded from the VAT breakdown.
- Buyer Reference falls back to the SI `po_no` (warned; XRechnung users
  should add a dedicated routing-ID field).

## Transmission lifecycle

```
generate_e_invoice ──► Generated ──► (Queued) ──► Transmitted
                          │   transmit()             ▲
                          └──────► Failed ───────────┘ (regenerate refreshes
                                                        the same log)
```

- `generate_e_invoice(sales_invoice, profile=None)`: validates, builds,
  attaches `{SI}-{profile}.xml` as a **private File** (replacing an earlier
  one of the same name) and creates/refreshes a log with status *Generated*.
- `download_e_invoice(...)`: same validation/build, streamed via
  `frappe.local.response` — no side effects.
- `transmit(log_name)`: profile `transport_mode == "Manual"` marks the log
  *Transmitted* (the operator delivers the XML out of band). `"API"` throws
  a Not Implemented error naming the adapter interface below.

Profile resolution order: explicit param (EDI Transmission Profile name or
raw UBL profile key) → company's default EDI Transmission Profile → any
profile of the company → bare `peppol-bis-3`.

## Provider adapter interface (extension point)

Real delivery to Peppol access points (e.g. via AS4) or national platforms
(SDI, Chorus Pro, ZATCA, KSeF, MyInvois) plugs in behind `transport_mode =
"API"`. Sketch for v2 — a registry of adapters keyed off the transmission
profile:

```python
class EDIProviderAdapter(ABC):
    """One instance per EDI Transmission Profile with transport_mode='API'."""

    def __init__(self, profile: "EDITransmissionProfile"): ...

    @abstractmethod
    def transmit(self, log: "EDITransmissionLog", xml: str) -> "TransmitResult":
        """Deliver the document. Return provider message id; raise on error.
        api.transmit() sets Queued before calling, then Transmitted/Failed
        from the result — adapters never mutate the log directly."""

    @abstractmethod
    def poll_status(self, log: "EDITransmissionLog") -> "ClearanceStatus":
        """For clearance regimes: submitted/cleared/rejected + platform ref."""

    def receive(self) -> "list[InboundDocument]":
        """Optional: pull inbound UBL → draft Purchase Invoice
        (generalizing regional/doctype/import_supplier_invoice)."""
```

Adapters register via a hook (`edi_provider_adapters`) so third-party apps
(e.g. an access-point connector) can contribute one without patching core.
Credentials live on the connector's own Settings doctype, not on the profile.

## Recommended UI wiring (not applied — see file scope)

`erpnext/public/js/` Sales Invoice form extension, gated on submitted docs:

```js
frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.doc.docstatus !== 1) return;
        frm.add_custom_button(__("Generate UBL E-Invoice"), () => {
            frappe.call({
                method: "erpnext.edi.ubl.api.generate_e_invoice",
                args: { sales_invoice: frm.doc.name },
                freeze: true,
                callback(r) {
                    if (r.message.warnings?.length)
                        frappe.msgprint({
                            title: __("Generated with warnings"),
                            message: r.message.warnings.join("<br>"),
                            indicator: "orange",
                        });
                    frm.reload_doc();
                },
            });
        }, __("Create"));
        frm.add_custom_button(__("Download UBL E-Invoice"), () => {
            open_url_post("/api/method/erpnext.edi.ubl.api.download_e_invoice",
                { sales_invoice: frm.doc.name });
        }, __("Create"));
    },
});
```

## Testing

`python erpnext/edi/ubl/test_ubl.py` — 26 pure tests: element order, endpoint
scheme attributes, tax math, currencyID attributes, monetary totals, line
values/unitCode, quantity trimming, validation failures (totals mismatch, bad
country codes, missing VAT), profile checks (XRechnung buyer-reference),
deterministic output, XML escaping. Mapper/api are exercised against a site
in integration testing (they need Sales Invoice fixtures).
