# AI Provider Core + AP Document Intake — Design Notes

Blueprint W6, "document intake" pillar: unstructured supplier invoices
(pasted text / e-mail body / uploaded text file) → LLM extraction →
validated staging document → **draft** Purchase Invoice. The structured
counterpart is `erpnext/edi/inbound/` (UBL e-invoices); this module is its
unstructured twin and inherits its safety doctrine verbatim.

## Layout (and why the doctypes are not in this directory)

```
erpnext/ai/                      pure/frappe-light python package (no module
  providers.py                   registration needed — it is just code)
  schemas.py                     pure: JSON schema + validation/coercion
  prompts.py                     pure: prompt templates
  api.py                         whitelisted endpoints (frappe adapter)
  test_ai_core.py                pure tests, MockProvider, no network

erpnext/utilities/doctype/       doctypes live under the registered
  ai_settings/                   **Utilities** module: Frappe resolves a
  ai_document_extraction/        doctype's code path as
  ai_document_extraction_item/   <app>/<scrub(module)>/doctype/<scrub(name)>,
  ai_document_extraction_tax/    and modules must be listed in
erpnext/utilities/report/        erpnext/modules.txt — which this change
  ai_extraction_queue/           set was not allowed to edit.
```

**Recommended follow-up** (one-line orchestrator edit): add `AI` to
`erpnext/modules.txt`, create `erpnext/ai/doctype/` + `erpnext/ai/report/`,
move the four doctype folders and the report there and flip their
`"module"` field to `"AI"`. The pure code already lives in `erpnext/ai/`,
so the move is mechanical.

## Provider abstraction (`providers.py`)

One abstract method: `complete(system_prompt, user_content,
response_json_schema=None, temperature=0.0, max_tokens=4000) -> str`.

- **`OpenAICompatibleProvider(base_url, api_key, model)`** — the
  `/chat/completions` dialect. **Sovereignty is the point**: this one class
  covers OpenAI, Azure-style proxies, *and* fully self-hosted vLLM, Ollama
  (`/v1`), LiteLLM and llama.cpp — invoice text never has to leave the
  operator's infrastructure. `api_key` is optional (keyless local servers).
  JSON discipline via `response_format: {type: "json_schema", strict}`.
- **`AnthropicProvider(api_key, model, base_url=https://api.anthropic.com)`**
  — Messages API. No `response_format` exists there, so the schema is
  enforced as a **forced tool call** (`tool_choice: {type: "tool"}`) whose
  `input_schema` is the extraction schema; the tool_use block's `input` *is*
  the JSON document.
- **`MockProvider`** — deterministic canned responses (list or callable),
  records every call; the only provider tests ever run.
- Transport is the `requests` library — the pattern ERPNext already uses for
  outbound HTTP (`currency_exchange_settings`, plaid). `frappe.integrations.
  utils.make_post_request` is not used anywhere in erpnext, so we did not
  introduce it.
- `get_provider()` is the **only** frappe-touching function: reads the
  **AI Settings** single (`api_key` via `get_password`), raises
  `ProviderNotConfigured` with a message naming AI Settings.

Payload/header construction is factored into pure functions
(`build_openai_payload`, `build_anthropic_payload`, `build_*_headers`,
`extract_*_text`) precisely so they are unit-testable with zero network.

## Extraction contract (`schemas.py`, `prompts.py`)

The LLM is an *extractor, never an author*. `SYSTEM_PROMPT_INVOICE` forbids
invention, requires `null` for absences and honest `field_confidences`
(0–1 per top-level field), which drive the review queue. The document text
is fenced between sentinel markers and declared untrusted, so
prompt-injection attempts inside an invoice stay identifiable as content.

`validate_extraction(raw_text) -> (data, errors, warnings)`:

- JSON repairs limited to the three real-world failure modes: markdown
  fences, prose around the object, trailing commas. Anything else → error.
- Date coercion to ISO: `YYYY-MM-DD`, `DD/MM/YYYY` / `MM/DD/YYYY` (both
  parts ≤ 12 → day-first reading **with an ambiguity warning**), `.`/`-`
  separators, `DD Mon YYYY`, `Mon DD, YYYY`.
- Amount coercion is locale-aware: `1,234.56`, `1.234,56` (continental —
  warned), `42,50`, `1.234.567`, currency symbols stripped.
- Totals cross-checks (Σlines vs `net_total`; `net+tax` vs `grand_total`)
  at 0.02 tolerance are **warnings, not errors** — the supplier's figures
  win, humans arbitrate.
- Confidence floor: fields below the threshold (document field
  `confidence_threshold`, default 80%) or missing a confidence go to the
  `needs_review` list → status **Needs Review**.

## Staging + safety doctrine (`api.py`)

Identical rules to `erpnext.edi.inbound.api`, some literally imported:

| Rule | Implementation |
|---|---|
| Never submit | `create_purchase_invoice` inserts a draft and stops |
| Never mint masters | supplier/item/account resolution only; blanks + warnings |
| Duplicate guard | refuse when a PI with same `supplier` + `bill_no` exists (docstatus < 2) — mirrored from the EDI module's private `_assert_no_duplicate_invoice` |
| Stage, don't discard | bad JSON → `Failed` with raw output kept; validation errors → `Needs Review`; never dropped |
| Reused imports | `resolve_expense_account`, `resolve_tax_account` from `erpnext.edi.inbound.mapper` (clean scalar signatures) |
| Mirrored (with reason) | `resolve_supplier` (EDI version is bound to its `ParsedInvoice` dataclass), duplicate guard (bound to `invoice_id` fieldname) |

Item matching is deliberately **absent in v1**: free text gives no reliable
basis (no supplier part numbers, unlike UBL), and a guessed item books to
the wrong expense account and stock ledger. The operator sets `item_code`;
supplier-specific learning from corrections is the v2 hook.

Provider failures never raise out of `extract_document`: status → `Failed`
with the message in `warnings`, making the function safe for
`frappe.enqueue` (`extract_document_async`, long queue, deduplicated job id).

## PDF decision (v1, honest)

No PDF text-extraction library is importable in this environment
(`pdfplumber`, `pypdf`, `PyPDF2`, `fitz` all absent; `frappe.utils.pdf` is a
*generator* built on pdfkit, not an extractor). Therefore v1 accepts text
sources only — `.txt`/`.csv`/`.md`/`.text` attachments are read via
`File.get_content()` (the pattern used by `chart_of_accounts_importer` and
`bank_statement_import_log`); `.pdf` is refused with an explicit message
telling the operator to paste the text. Adding `pypdf` to pyproject
dependencies is the natural v1.1.

## Recommended hooks (documented, NOT applied — hooks.py is out of scope)

- **Email inbox intake**: an Email Account linked to a small
  `on_communication` handler that creates `AI Document Extraction`
  (source_type Email, raw_text = body + text attachments) and calls
  `extract_document_async`.
- **Scheduler retry**: hourly job re-enqueueing `Failed` documents younger
  than N days (provider outages are transient).
- **Workspace/`modules.txt`**: register the `AI` module and move the
  doctypes (above).

## Testing

`python erpnext/ai/test_ai_core.py` — 45 pure tests, importlib bootstrap
(same as `edi/inbound/test_inbound.py`), zero network, zero frappe.
