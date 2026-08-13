# Beating Oracle ERP: Gap-Closure Execution Blueprint

Companion to [`ORACLE_ERP_COMPARISON.md`](./ORACLE_ERP_COMPARISON.md). That document says *what* is missing; this one says *how to build it* — concrete module designs grounded in substrates that already exist in this codebase, a sequencing plan, and the strategy that makes "beat Oracle" a winnable statement rather than a slogan.

---

## 1. The theory of victory

Nobody beats Oracle by cloning Oracle. Fusion is 20+ years of accumulated statutory depth sold to Fortune-500 buyers who value indemnification more than software. The winnable war is different:

1. **Win the mid-market decisively.** The real competitors in 80% of deals are NetSuite (Oracle's own mid-market product), Dynamics 365 BC, and SAP Business One. Every gap closed below is chosen because it loses mid-market deals today, not because Oracle has it.
2. **Be AI-native where Oracle is AI-bolted-on.** Oracle retrofits AI onto a closed schema. This codebase has a typed, auto-generated REST surface over every doctype and full code transparency — structurally better substrate for agentic AI. This is the one dimension where leapfrogging (not catching up) is possible.
3. **Weaponize TCO and time-to-value.** Every feature below must be usable in hours with shipped defaults, not consultant-months. Oracle cannot follow us down this curve; its margin structure forbids it.
4. **Compliance as product, not paperwork.** Mid-market companies fail audits on leases, rev rec, SoD, and e-invoicing mandates. Shipping those four turns "open-source ERP" from a CFO objection into a CFO reason-to-buy.

**Definition of "beats Oracle":** within 8 quarters, a mid-market manufacturer or multi-entity services company can run audit-clean books (ASC 606/842, multi-GAAP, SoD evidence), plan and promise orders against finite capacity, execute a bin-level warehouse, and file e-invoices in mandated countries — at ~1/10th Oracle's 5-year TCO, live in weeks, with AI doing the data entry.

---

## 2. Architecture doctrine (applies to every workstream)

These rules come from patterns already proven in this repo:

- **Pure-core + adapter.** The scheduling engine (`erpnext/manufacturing/scheduling/`: `engine.py`, `models.py`, `plan_adapter.py`) is Frappe-free domain logic behind thin adapters. Every new engine below (forecasting, allocation, ATP, eliminations, tax) follows this shape: unit-testable, swappable, reusable outside Frappe.
- **Orchestrate existing doctypes; don't duplicate them.** The close manager schedules existing PCV/revaluation/deferred runs; lease accounting *generates* existing `asset_depreciation_schedule`-style schedules; encumbrance reuses the existing budget controller's hooks. New tables only where a new noun genuinely exists.
- **Ledger-adjacent = in-core; vertical = app.** Lease, rev rec, multi-GAAP, encumbrance touch `gl_entry` and belong in `erpnext/`. WMS execution UI, forecasting workbench, and AI services can be separate apps on the monorepo pattern the `banking/` SPA established.
- **Modern UX via the banking-SPA pattern.** React 19 + Vite building to `/assets/erpnext/<app>/`, served by a `www/` route (`hooks.py:222`). Every new operational surface (warehouse handheld, planning workbench, close cockpit, Gantt) uses this stack, not Desk forms.
- **Everything ships content.** Fixtures for workflows, SoD rules, KPI packs, country e-invoice profiles. Oracle sells configuration; we ship it.
- **Background-first.** Heavy runs (allocation, forecasting, eliminations) follow the `process_period_closing_voucher` chunked-background pattern with status fields, never request-thread work.

---

## 3. Workstream designs

### W1 — Financial platform (in-core, `erpnext/accounts` + `erpnext/assets`)

**1a. Cash-flow forecasting** *(the fastest CFO-visible win)*
- New report + doctype `Cash Flow Forecast` with child `Cash Flow Forecast Line`.
- Sources, all already in the schema: unpaid `payment_schedule` rows on SI/PI (due dates + early-pay discount windows), unbilled SO/PO pipelines, `subscription` next-billing dates, recurring `journal_entry_template` patterns, `payment_order` batches, opening balances from `bank_account_balance`.
- Pure-core projection engine (`accounts/forecasting/engine.py`): bucket by day/week/month, per-bank-account and consolidated, with scenario overlays (delay-days on receivables, FX shift via `currency_exchange_settings` rates).
- v2: ML payment-date prediction per customer from historical `payment_entry` lag (feeds from W6).

**1b. Lease accounting (ASC 842 / IFRS 16)**
- Doctypes: `Lease Contract` (lessee/lessor role, term, payments child table, discount rate, options), `Lease Modification`.
- On submit: compute lease liability (PV of payments) and create a **standard `asset`** with `asset_category` "Right of Use" — CWIP, depreciation books, and disposal flows come free from the existing assets module.
- Monthly run (pattern: `process_deferred_accounting`): interest accretion JE + liability reduction; modifications remeasure and post the delta.
- Reports: maturity analysis, ASC 842 disclosure pack. Finance-book aware from day one so IFRS 16 vs. local-GAAP operating treatment diverge per book (see 1d).

**1c. Revenue management (ASC 606 / IFRS 15)**
- Doctypes: `Revenue Contract` (auto-created from SO/SI), `Performance Obligation` (child: linked items, SSP, satisfaction method point-in-time/over-time), `SSP Catalog` (per item/group, observable or estimated).
- Pure-core allocation engine: relative-SSP allocation of transaction price across obligations; contract-modification handling (prospective vs. cumulative catch-up).
- Recognition run posts via the **existing deferred-revenue machinery** (`deferred_revenue.py` + `process_deferred_accounting`) — obligations map to service start/end or event triggers (delivery from DN, milestone from Task).
- Five-step audit trail rendered on the contract for auditors.

**1d. Multi-GAAP: promote `finance_book` to a real parallel ledger**
- The GL already carries four currency layers per entry; books are the missing axis.
- New doctype `Accounting Policy Rule`: (finance book × doctype/event × account mapping × treatment). Posting flows through a book-expansion layer in the GL map: entries with `finance_book = None` remain common to all books; policy rules generate book-specific deltas (e.g., IFRS 16 capitalization vs. local-GAAP rent expense, tax vs. book depreciation — the latter already works today via `asset_finance_book`).
- Balance assertion per book; `financial_statements.py` already filters by finance book, so reporting is nearly free.
- This is the highest-risk item in W1 — gate it behind a company-level opt-in flag and reposting tooling (pattern: `repost_accounting_ledger`).

**1e. Close management**
- Doctypes: `Close Cycle` (company/companies, period, template), `Close Task` (owner, depends_on, due offset, sign-off + attachment, task type).
- Task types bind to existing actions: run PCV, run `exchange_rate_revaluation`, run deferred accounting, verify `bank_reconciliation_statement` gap = 0, `ledger_health_monitor` clean, custom checklist.
- Auto-verification where possible (the bound doctype's status *is* the evidence). Close cockpit SPA: burn-down, blockers, per-entity swimlanes.
- Balance-sheet reconciliation sign-off: `Account Reconciliation Statement` (account, period, GL balance snapshot, substantiation, approver) — the ARCS counter.

**1f. Consolidation engine**
- Extend `Company` with ownership structure (parent %, method: full/equity).
- Doctype `Elimination Rule` (IC account pairs, matching by `party_link`/inter-company references already stamped on JEs and invoices).
- Consolidation run: currency translation (closing/average per `Account.root_type`), auto-generated elimination JEs posted into a dedicated consolidation `finance_book` (from 1d), minority-interest computation, CTA account.
- The existing `consolidated_financial_statement` report then reads book-filtered data instead of naive summation.

**1g. Encumbrance (public-sector option)**
- The budget controller already intercepts MR/PO/actuals with Stop/Warn. Add an `enable_encumbrance_accounting` company flag: PO submit posts commitment entries to an encumbrance finance book; invoice/receipt relieves them. Funds-available = budget − encumbrance − actual, already expressible in `budget_variance_report`.

### W2 — GRC (in-core, small, disproportionate sales impact)

- **`SoD Rule`** doctype: two conflicting capability sets (role+doctype+permlevel patterns), risk rating, shipped fixture pack (~30 classic conflicts: create supplier + pay supplier; maintain prices + approve credit; post JE + approve JE…). Scanner job cross-joins `Has Role`/User Permissions → `SoD Violation` log with mitigation/exception workflow and an auditor export. Read-only over existing tables — weeks, not months.
- **Shipped workflow content**: fixture pack of Frappe `Workflow` records — PO approval above threshold (threshold in Buying Settings), JE approval, credit-limit release, price-change approval — installable per company from a settings page. Closes "ERPNext ships zero workflows" instantly.
- **Audit workspace**: consolidated view over the 309 `track_changes` doctypes — who changed master data, permission changes, override-role usage (every `role_allowed_to_*` bypass event logged to one `Control Override Log`).

### W3 — Warehouse & order promising

**3a. WMS-lite (target: 95% of mid-market warehouses, not LogFire)**
- `Storage Location` tree per warehouse (zone→aisle→rack→bin), first-class doctype (not just `inventory_dimension`, which stays for exotic dimensions) with capacity, pick sequence, and type (pick/bulk/staging/QC).
- Stock ledger gains a location dimension via the existing `inventory_dimension` machinery pointed at `Storage Location` — the substrate was built for exactly this.
- Directed putaway: extend `putaway_rule` to resolve to locations (capacity + priority logic already exists).
- Directed picking: `pick_list.set_item_locations` orders by location pick sequence; batch/expiry logic unchanged.
- `Cycle Count Program`: ABC classification job (velocity from SLE), schedule generation producing draft `stock_reconciliation` scoped to locations, accuracy KPIs.
- **Handheld PWA** (banking-SPA stack): scan-driven receive/putaway/pick/count/transfer against existing endpoints (`scan_barcode` fields already exist on pick list/stock entry). This UI is half the perceived "WMS" value.

**3b. ATP/CTP order promising**
- Pure-core promise engine (`stock/promising/engine.py`): supply timeline per item/warehouse from `bin` + open PO/WO/production-plan receipts (all queryable today) minus reservations (`stock_reservation_entry`), yielding earliest-date-for-qty.
- SO item gets `promised_date` + one-click "Promise" that consumes ATP; CTP for make-items calls the **existing scheduling engine** in dry-run mode to get a finite-capacity completion date — the two engines compose.
- Re-promise report when supply slips (PO reschedules, WO delays).

**3c. E-invoicing / CTC framework (on the EDI module — regulatory tailwind)**
- `edi/ubl/`: UBL 2.1 / Peppol BIS generator mapping ERPNext fields through `common_code` (UOM, tax categories, payment means — the genericode importer already loads the UN/CEFACT lists).
- `Transmission Profile` doctype + provider adapter interface (Peppol access points, national platforms); per-country profile packs as fixtures (start: DE/FR XRechnung-Factur-X, IT reusing FatturaPA code, MY, SA ZATCA, IN via india_compliance alignment).
- Inbound: received UBL → draft PI (generalizing `import_supplier_invoice`). Status lifecycle (submitted/cleared/rejected) on the invoice.

### W4 — Planning science

**4a. Statistical forecasting (replace the `demand_qty = 1.0` scaffold)**
- Pure-core `forecasting/` package: Holt-Winters (triple exponential, additive/multiplicative), Croston/SBA for intermittent demand, naive/seasonal-naive baselines. No heavy deps — these are ~200 lines each, keeping the no-numpy discipline.
- Backtesting harness: rolling-origin evaluation over SLE/SO history, per-item champion model by MAPE/bias; `Forecast Run` doctype stores accuracy so planners see trust levels.
- Output writes `sales_forecast` rows — MPS (`get_mps_data`) and the MRP report consume them **unchanged**.
- Planning workbench SPA: exception-driven (items whose forecast/actual diverge), override with reason codes, consensus notes.

**4b. Scheduler phases 3–5 (finish what's started)**
- Per its own `DESIGN.md`: full WO/job-card date sync (3), then interactive **Gantt board** SPA on the engine's what-if API — drag = dry-run repair proposal, apply = persist (5). Sequence-dependent setup times and alternate-resource preference weights as engine increments (4).
- This plus 4a plus 3b yields the demo Oracle can't match at this price point: forecast → MPS → MRP → finite schedule → order promise, one system, no integration seams.

### W5 — Source-to-contract

- `Sourcing Event`: RFx over the existing RFQ supplier-portal plumbing — sealed bids until close, multi-round, line/lot awards, side-by-side award scenarios scored by the **supplier scorecard engine** (price + scorecard standing + qualification status).
- `Supplier Qualification`: questionnaire templates, document requirements with expiry tracking (insurance, ISO certs), risk tier; gates PO creation optionally.
- Contract 2.0 on the existing `Contract` doctype: `Clause Library` with versioned clauses, assembled documents, **obligations as SLA-driven items** — the generic SLA engine (`service_level_agreement.apply` fires on any doctype via the wildcard hook) gives obligation reminders/escalation for free. Renewal pipeline = expiry-driven kanban.

### W6 — AI-native layer (the leapfrog; separate app `erpnext_ai`, model-agnostic)

Provider abstraction first (self-hosted or API LLMs — data-sovereignty is *our* differentiator, so never hard-wire a vendor):

- **Document intake**: email/upload → LLM extraction → draft PI / bank transaction / expense with per-field confidence; below-threshold fields route to a review queue. Learns supplier-specific mappings from corrections. (Oracle IDR counter; also the single biggest data-entry pain in the segment.)
- **Reconciliation copilot**: candidate ranking for bank matching — features from `bank_transaction_rule` history, `party_link`, fuzzy amounts/dates — surfaced in the existing banking SPA as one-click suggestions with explanations.
- **Anomaly & duplicate detection**: nightly screens (duplicate invoices, unusual account/dimension combos, off-trend postings) feeding the `ledger_health_monitor` surface that already exists.
- **Payment-date prediction** feeding the cash forecast (1a).
- **Agent surface**: an MCP server over the Frappe REST/schema layer with **guardrails as policy, not prompt** — agents create drafts only, submits require the W2 workflow approvals, every agent action logged to the audit workspace. "Close my books" as an agent orchestrating the W1e close cycle is the marquee demo.
- Forecast narrative/explanations on 4a outputs.

### W7 — Analytics

- **Semantic layer**: `Metric` doctype (measure, dimensions, grain, SQL/query-object binding) with a governed catalog; shipped KPI packs per module (DSO, DPO, inventory turns, OTIF, forecast accuracy, close duration). Report Center (already scaffolded in-repo) becomes the front door.
- **Warehouse sidecar**: opt-in replication to DuckDB/ClickHouse star schemas for heavy analysis; keeps the transactional DB honest and answers the "FDI prebuilt warehouse" objection.
- Embed/align with the Frappe Insights app rather than building a BI tool.

---

## 4. Sequencing

Eight quarters, four releases. Each release must ship *sellable* capability, not scaffolding. (Team-size assumption: ~8–12 strong engineers + 2 domain/product; scale confidence, not scope, if smaller.)

| Release | Theme | Ships | Effort ballpark |
|---|---|---|---|
| **R1 (Q1–Q2)** — "Trustworthy & Predictable" | Quick wins on existing substrates | Cash forecast v1 (1a) · SoD rules + scanner (W2) · workflow fixture pack (W2) · close manager v1 (1e) · statistical forecasting + accuracy (4a) · scheduler phase 3 | ~10 eng-quarters |
| **R2 (Q3–Q4)** — "Audit-Clean & Promised" | Compliance + promising | Lease accounting (1b) · rev rec v1 (1c) · ATP/CTP (3b) · Gantt board (4b/phase 5) · e-invoicing core + 3 countries (3c) · AI document intake beta (W6) | ~14 eng-quarters |
| **R3 (Q5–Q6)** — "Warehouse & Books at Scale" | Execution depth | WMS-lite + handheld PWA (3a) · multi-GAAP finance books (1d) · consolidation engine (1f) · reconciliation copilot + anomaly screens (W6) · semantic layer v1 (W7) | ~16 eng-quarters |
| **R4 (Q7–Q8)** — "Strategic depth" | Win bigger deals | Sourcing + qualification + CLM (W5) · encumbrance (1g) · agent surface GA (W6) · warehouse sidecar + KPI packs (W7) · e-invoicing country expansion | ~14 eng-quarters |

Dependency spine: **1d (finance books) before 1f (consolidation) and after 1b/1c ship in single-book mode** — lease and rev rec must not wait on the riskiest item. ATP (3b) needs nothing new; CTP needs scheduler phase 3. AI intake needs W2 workflows for its approval guardrails.

### Deliberate non-goals (partner, don't build)
Full TMS/GTM (integrate carriers/screening providers), global payroll (HRMS app + local providers), CPQ, F500-scale infrastructure re-platforming. Each would consume multiple releases and defend Oracle's strongest ground instead of attacking its weakest.

---

## 5. Non-code work that decides the outcome

Software alone doesn't beat Oracle in a CFO's office:

1. **Compliance evidence**: SOC 2 / ISO 27001 for the managed cloud; auditor-facing feature docs (rev rec method papers, SoD evidence exports). Budget this like an engineering workstream.
2. **Migration weapons**: importers targeting NetSuite/Dynamics/SAP B1 exports (CoA, open AR/AP, items, BOMs, balances) — the switching cost *is* the moat we must melt.
3. **Reference verticals**: pick two (discrete manufacturing; multi-entity services) and make every R1–R3 demo tell their story end-to-end.
4. **Benchmarked claims**: publish TCO and implementation-time studies per release; "10x cheaper, 10x faster, audit-clean" only works with receipts.

---

## 6. First concrete steps in this repo

If we start tomorrow, the first three PRs are:

1. **SoD Rule + scanner + fixture pack** (`erpnext/accounts/doctype/sod_rule`, report `sod_violations`) — pure read over existing permission tables; small, shippable, instantly demo-able.
2. **Workflow fixture pack** (`erpnext/setup/fixtures/workflows/` + settings-page installer) — content, not code.
3. **Cash Flow Forecast engine + report** (`erpnext/accounts/forecasting/` pure core + report/SPA) — the CFO demo.

Each lands independently on `develop`, follows the pure-core + adapter doctrine, and starts compounding toward R1.
