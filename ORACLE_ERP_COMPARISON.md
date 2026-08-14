# ERPNext vs. Oracle ERP — Gap Analysis & Improvement Scope

**Repo analyzed:** `gold1314/erpnext`, branch `develop` (ERPNext `17.x.x-develop`, per `erpnext/hooks.py`)
**Comparison baseline:** Oracle Fusion Cloud ERP (Financials, Procurement, PPM, Risk Management) plus the adjacent Oracle Cloud SCM and EPM pillars that enterprises typically buy with it. Oracle E-Business Suite is referenced where relevant.
**Method:** The findings below are grounded in a code-level inventory of this repository (doctypes, reports, services, hooks), not marketing descriptions.

---

## 1. Executive summary

ERPNext in this repo is a genuinely complete **SMB/mid-market ERP core**: a real double-entry GL with multi-currency and reporting-currency layers, AR/AP with dunning and auto-reconciliation, budgeting with hard commit-time controls, fixed assets with multi-book depreciation and CWIP, full inventory with batch/serial/reservation, procurement with supplier scorecards, multi-level BOM/MRP/MPS manufacturing with a brand-new finite-capacity scheduling engine, projects, CRM, support with a strong SLA engine, POS, and subscription billing — all on one unified, open-source data model.

Compared to Oracle ERP, the gaps cluster into five themes:

1. **Statutory/enterprise accounting depth** — no lease accounting (ASC 842/IFRS 16), no ASC 606 revenue-management engine, no true secondary ledgers/multi-GAAP subledger rules engine, consolidation is a report rather than a close process, no encumbrance accounting.
2. **Governance, risk & compliance** — no segregation-of-duties analysis, no continuous controls monitoring, no financial-close orchestration; controls exist only as scattered role-based gates.
3. **Advanced supply chain** — no WMS (bins/waves/labor/RF), no TMS/GTM, no ATP/CTP order promising, no statistical or ML demand planning, no multi-echelon supply planning.
4. **Source-to-contract** — no sourcing events/auctions, no supplier qualification workflows, no contract lifecycle management with clause libraries.
5. **Analytics & AI** — solid transactional reports but no semantic layer/warehouse equivalent to OTBI/Fusion Data Intelligence, and zero ML/LLM capability in the codebase.

Conversely, ERPNext structurally beats Oracle on **TCO, transparency, customization velocity, deployment freedom, and time-to-value** — and several of the gaps above are natural roadmap items on substrates that already exist in this repo (the new scheduling engine, the EDI code-list module, `inventory_dimension`, the Frappe workflow engine).

---

## 2. What this repository actually contains

21 modules (`erpnext/modules.txt`): Accounts, CRM, Buying, Projects, Selling, Setup, Manufacturing, Stock, Support, Utilities, Assets, Portal, Maintenance, Regional, ERPNext Integrations, Quality Management, Communication, Telephony, Bulk Transaction, Subcontracting, EDI.

Notable recent additions found at HEAD:

- **Capacity-aware production scheduling** (`erpnext/manufacturing/scheduling/`): a Frappe-independent scheduling core with FORWARD/BACKWARD direction, FINITE/INFINITE modes, resource calendars, capability-based workstation selection, and dry-run what-if proposals. Its own `DESIGN.md` states it is modeled on Epicor Kinetic; phases 1–2 are implemented, Gantt boards (phase 5) are not.
- **Banking SPA** (`/banking` at repo root): a React 19 + Vite single-page app for bank reconciliation and statement import (CSV, Google Sheets, MT940, and PDF with bounding-box table extraction), with rule-based auto-classification (`bank_transaction_rule`) and fuzzy matching.
- **EDI module** (`erpnext/edi/`): `Code List` + `Common Code` doctypes with an OASIS genericode importer — the mapping substrate for Peppol/UBL-style e-invoicing, though no transmission logic lives here yet.
- **Subcontracting inward (job work)**: receiving customer-owned material as the subcontractor (`subcontracting_inward_order`).
- **Standard Cost valuation** joins FIFO / Moving Average / LIFO (`item_standard_cost`, `stock_settings`).
- **Master Production Schedule + Sales Forecast** doctypes feeding a real multi-level MRP report.
- **Workspace Sidebar / Report Center** fixtures — a new navigation and curated-reports layer.

Equally important is what is deliberately **not** here (moved to sibling Frappe apps, confirmed by deletion patches under `erpnext/patches/`): HR & Payroll (`frappe/hrms`), e-commerce storefront (`frappe/webshop`), payment gateway drivers (`frappe/payments`), Shopify/WooCommerce/Amazon connectors (`ecommerce_integrations`), India localization (`india_compliance`), TaxJar, Exotel, Healthcare, Education, Agriculture, Loan Management. Any fair comparison with Oracle's suite must count these apps — but they are separate installs, separate release trains, and separate quality bars.

---

## 3. Domain-by-domain comparison

Legend for the "Gap" column: 🟢 near-parity for the target segment · 🟡 partial — workable but shallower · 🔴 absent.

### 3.1 General Ledger & accounting foundation

| Capability | Oracle Fusion ERP | This repo | Gap |
|---|---|---|---|
| Chart of accounts | Multi-segment flexfields, cross-validation rules, hierarchies | Tree CoA + `account_number`, 72 country templates, CSV importer, account categories | 🟢 |
| Multi-currency | Ledger currency + reporting currencies, revaluation, translation | GL Entry carries account, company, transaction **and reporting currency** amounts; `exchange_rate_revaluation`; pegged currencies | 🟢 |
| Dimensions | Segment-based; data access sets | `accounting_dimension` (any doctype becomes a dimension) + per-account dimension restrictions | 🟢 |
| Multi-book / multi-GAAP | Primary + secondary ledgers, ledger sets, subledger accounting rules engine (SLA/Accounting Hub) | `finance_book` exists but is effectively **depreciation-books only**; no secondary ledger, no accounting rules engine | 🔴 |
| Intercompany | AGIS: balancing rules, IC agreements, netting, transfer pricing hooks | SI↔PI / SO↔PO mirroring, IC journal references, `party_link`, internal-transfer rate policing | 🟡 |
| Period close | Close Manager task orchestration, ledger period statuses | `accounting_period` (per-doctype soft close + exempted role), Period Closing Voucher with chunked background processing, account freeze dates | 🟡 |
| Ledger integrity | — (assumed) | `ledger_health_monitor`, `bisect_accounting_statements` (binary-search for when books broke), repost tools | 🟢 (genuinely novel) |
| Encumbrance / budgetary control | Full encumbrance accounting, funds checking | Budget doctype with Stop/Warn/Ignore at **Material Request, PO, and actual booking** — commitment-time control without encumbrance GL entries | 🟡 |

**Verdict:** the GL core is strong for its segment. The two structural gaps are the absence of a real **secondary-ledger/multi-GAAP mechanism** (IFRS + local GAAP + tax books posting differently from one event) and **encumbrance accounting** for public-sector-style funds control.

### 3.2 AR / AP / collections

Oracle: Advanced Collections (strategies, scoring), credit management, dynamic discounting, Intelligent Document Recognition (ML invoice capture), ISO 20022 payment formats.

This repo: full invoicing (incl. POS invoices with merge logs), payment terms with **early-payment discounts**, payment reconciliation with background **auto-reconciliation and fuzzy party matching**, `dunning` with interest/fees and multi-language letters, per-company credit limits with credit-controller bypass roles, scheduled emailed statements of account (`process_statement_of_accounts`), invoice discounting (receivables financing), `payment_order` for bank payment batches.

**Gaps:** 🔴 AP invoice capture/OCR automation; 🔴 collections strategies/worklists beyond dunning; 🟡 payment-file formats (no ISO 20022/pain.001 generation in core); 🔴 dynamic discounting marketplace mechanics (static early-pay discounts exist).

### 3.3 Cash & banking

Oracle: bank statement auto-rec, cash positioning, ML cash forecasting.

This repo: this is a **relative bright spot** — modern React reconciliation UI, rule engine, MT940/CSV/Google Sheets/**PDF** statement import, Plaid feed sync, bank clearance, payment orders.

**Gaps:** 🔴 cash positioning and cash-flow **forecasting** (there is a historical Cash Flow statement, but no forward-looking cash forecast); 🟡 direct bank connectivity beyond Plaid (EBICS, ISO camt.053 not in core).

### 3.4 Fixed assets

Oracle: asset books (corporate/tax), mass transactions, impairments, leases.

This repo: full lifecycle (12 statuses), multi-book depreciation per asset (SL/DDB/WDV/manual, shift-based allocation), CWIP accounting, capitalization from stock/services/other assets, repairs, movements, its own maintenance-team subsystem, value adjustments (impairment-like).

**Gaps:** 🔴 **lease accounting (IFRS 16 / ASC 842)** — grep-confirmed zero lease code: no right-of-use assets, lease liabilities, or amortization schedules. This is the single most commonly-audited missing accounting feature. 🟡 mass asset transactions/revaluation campaigns.

### 3.5 Revenue recognition & subscriptions

Oracle: Revenue Management Cloud — ASC 606/IFRS 15 five-step model, performance obligations, SSP allocation, contract modifications.

This repo: deferred revenue/expense with monthly/daily proration and automated posting; a capable `subscription` engine (trials, calendar alignment, past-due invoice policy, gateway-linked payment requests).

**Gaps:** 🔴 multi-element arrangement allocation (SSP), performance obligations, contract-modification reallocation. Deferred-revenue proration is not ASC 606 compliance for any business with bundled deliverables.

### 3.6 Tax & statutory localization

Oracle: Fusion Tax engine (determinants, place-of-supply rules, exception hierarchy), partner integrations (Vertex/Avalara), country localizations maintained by Oracle.

This repo: template-driven taxes + `tax_rule` auto-selection (by geography/category/priority), item tax templates, robust **withholding tax** (categories, cumulative thresholds, LDC certificates), 72 country CoAs, but only **6 regional modules in-core** (IT, AE, ZA, US-1099, AU, TR-stub); India, KSA, Germany-DATEV etc. moved to external apps. E-invoicing exists only as Italy FatturaPA XML plus the new EDI code-list substrate.

**Gaps:** 🟡 no rules-engine tax determination beyond `tax_rule` matching; 🔴 no in-core e-invoicing/CTC framework (Peppol, SAF-T, real-time reporting) — increasingly mandatory across the EU, LATAM, and GCC; 🟡 localization coverage depends on third-party apps of varying quality.

### 3.7 Consolidation, close & EPM

Oracle: FCCS (eliminations, ownership/minority interest, currency translation, close task management), ARCS (account reconciliations), EPM Planning.

This repo: `consolidated_financial_statement` report (multi-company columns, presentation currency), consolidated trial balance, intercompany mirroring. Budgeting exists at control level; no planning workbench.

**Gaps:** 🔴 automated **eliminations**, ownership %/minority interest, equity pickup; 🔴 close checklist/task orchestration; 🔴 account reconciliation management (balance-sheet cert workflows); 🔴 driver-based planning/forecasting (budgets are static amounts with monthly distribution).

### 3.8 Procurement & sourcing

Oracle: Self-Service Procurement, Sourcing (RFx, reverse auctions, award analysis), Supplier Qualification Management, Procurement Contracts (clause library, deviations), Supplier Portal + Business Network.

This repo: MR→RFQ→Supplier Quotation (with a real **supplier portal** quoting page)→PO, blanket orders, and an unusually deep **supplier scorecard** (~25 computed variables, weighted criteria, standings, scheduled recalc).

**Gaps:** 🔴 sourcing events/auctions and award optimization; 🔴 supplier qualification/onboarding workflows (registration, document collection, risk screening); 🔴 CLM — the generic `Contract` doctype has a fulfilment checklist but no clause library, versioning/redlining, obligations, or renewals pipeline; 🟡 punchout/catalog buying (no OCI/cXML).

### 3.9 Inventory & warehouse management

Oracle: Inventory + Cost Management, and a real WMS (waves, RF task queues, labor management, license plates, slotting) via Oracle WMS Cloud.

This repo: warehouse tree, batch/serial via the new `serial_and_batch_bundle`, four valuation methods with retroactive repost, **stock reservation down to serial/batch**, pick lists with barcode scan mode and auto-allocation, putaway rules (capacity+priority), quality inspection with formula-based acceptance criteria, `inventory_dimension` for user-defined stock dimensions, landed cost (incl. vendor invoices), stock closing entries.

**Gaps:** 🔴 confirmed by grep — no wave picking, no task interleaving, no cross-docking, no slotting, no cycle-count programs, no labor management, no license plates. `bin` is a quantity aggregate, not a location. ERPNext is inventory management; Oracle WMS is warehouse execution. For any operation beyond ~1 pick zone this is the biggest operational gap.

### 3.10 Order management & pricing

Oracle: Distributed Order Orchestration, **Global Order Promising (ATP/CTP)**, pricing strategies/algorithms, channel management.

This repo: quotation→SO→DN with delivery schedule lines, product bundles, drop shipping, proforma invoices, pricing rules + promotional schemes (buy-X-get-Y) + coupons + loyalty, strong POS (profiles, shift open/close, offline merge logs).

**Gaps:** 🔴 ATP/CTP — promised dates are user-entered; nothing checks supply against the delivery date at order entry (stock *reservation* exists, order *promising* does not); 🔴 multi-channel order orchestration; 🟡 pricing lacks approval workflows/margin guardrails.

### 3.11 Manufacturing & planning

Oracle: discrete/process manufacturing, Supply Planning, Demand Management (statistical + ML forecasting), S&OP, Production Scheduling.

This repo: multi-level BOMs (+ explosion table, BOM creator, mass update tools), work orders with rich job cards (sub-operations, corrective job cards, semi-finished tracking, process loss, downtime), workstations with costing and a **plant floor** dashboard, Production Plan with multi-level sub-assembly netting, MPS, an MRP report with lead-time offsetting and supply netting, and the new **finite-capacity scheduling engine** (forward/backward, what-if dry runs).

**Gaps:** 🟡 the scheduler is phases 1–2 of 5 (no Gantt boards, no drag-reschedule); 🔴 demand forecasting is a manual scaffold — `sales_forecast.generate_demand()` literally fabricates `demand_qty = 1.0` rows; the only statistics anywhere is single exponential smoothing in one report. No forecast accuracy tracking, no seasonality, no consensus planning; 🔴 no process manufacturing (formulas/potency/co-product cost allocation is limited to BOM secondary items); 🔴 no S&OP layer; 🟡 no MES-grade shop-floor execution (job cards get close for simple flows).

### 3.12 Logistics & trade

Oracle: OTM (rating, tendering, load building, fleet), GTM (restricted-party screening, HTS classification, duty).

This repo: `delivery_trip` multi-stop routing with Google Maps optimization, `shipment` as a carrier-booking wrapper (AWB, tracking, incoterms, parcels), `customs_tariff_number` on items.

**Gaps:** 🔴 freight rating/tendering/load building; 🔴 trade compliance (denied-party screening, export licensing, duty calculation). For most SMBs a carrier-integration app suffices, but this is a hard ceiling for logistics-intensive users.

### 3.13 Projects (PPM)

Oracle: Project Financials (budgets/forecasts, capitalization, contract billing methods, revenue recognition per project), Resource Management (capacity, staffing), Grants.

This repo: projects with real **costing and margin roll-ups** (purchase cost, consumed material, timesheet cost vs. billed), submittable multi-currency timesheets billable into Sales Invoices, task trees with dependencies/milestones, Gantt/Kanban via framework views, project templates and status-update automation.

**Gaps:** 🔴 resource capacity planning/staffing (no allocation, leveling, skills, utilization targets — Employee is a stub here; HRMS holds the rest); 🔴 project revenue recognition (POC methods) and contract-based billing rules (milestone/rate/fixed schedules beyond manual invoicing); 🟡 no EVM (earned value), no project budgets separate from the accounts Budget doctype.

### 3.14 CRM & service

Oracle sells CX separately (Sales/Service Cloud); Fusion ERP itself has little CRM — ERPNext including CRM in-suite is a **plus**. Pipeline (lead→opportunity with UTM attribution, competitors, sales stages), public appointment booking with email verification, and a genuinely deep **SLA engine** (1,060 lines: business-hours windows, holidays, pause states, timezone-aware, appliable to *any* doctype). Warranty claims tie to serials and maintenance visits. Note the ecosystem direction: in-core CRM now ships a sync bridge to the standalone Frappe CRM app.

**Gaps vs. dedicated CRM/Service:** 🟡 marketing automation (email campaign is minimal), 🟡 field-service dispatch/scheduling, 🔴 CPQ.

### 3.15 HR & payroll

Not in this repo at all (deletion patches confirm the move to `frappe/hrms`); only the Employee master stub remains in `setup`. Oracle HCM is a first-party integrated pillar with global payroll. Anyone evaluating "this repo" as their ERP must plan the HRMS app as a second install — including for expense claims, which also live there.

### 3.16 Quality & maintenance (EAM)

This repo splits quality in two: transaction-gate **inspections** (in Stock, formula-based criteria, purchase/delivery gates) and an ISO-style **QMS** module (procedures tree, goals/reviews, non-conformance, CAPA, quality meetings). Maintenance module is thin (schedule + visit for sold items); asset-side PM lives in `asset_maintenance_task`.

**Gaps vs. Oracle Quality/Maintenance Cloud:** 🔴 SPC/control charts, sampling plans (AQL), MRB dispositions tied to lots; 🔴 meter-based/condition-based PM, maintenance work orders with labor+spares costing, spare-parts planning.

### 3.17 Risk, controls & audit

Oracle Risk Management Cloud does SoD analysis (Advanced Access Controls) and transaction monitoring (Advanced Financial Controls); everything routes through BPM approval workflows.

This repo's control surface: `track_changes` on **309 doctypes** (version audit trail), optional **immutable ledger** (cancel-by-reversal), auto-cancel-exempt ledger doctypes, frozen accounts/periods with modifier roles, ~8 scattered override roles (credit controller, over-billing, internal-transfer rate override, accounting-period exemption…), dimension-value restrictions per account, and multi-company data isolation (`company_restriction`). ERPNext ships **zero standard workflows** — approvals depend on each site configuring Frappe's Workflow engine.

**Gaps:** 🔴 SoD conflict definition/detection across roles; 🔴 continuous controls monitoring; 🔴 shipped approval-policy content; 🟡 no consolidated audit-trail reporting for auditors.

### 3.18 Analytics, reporting & AI

Oracle: OTBI ad-hoc analysis on a semantic layer, BI Publisher statutory output, Fusion Data Intelligence (prebuilt warehouse/KPIs), Smart View for Excel, and an accelerating stream of embedded AI (IDR invoice capture, ML cash forecasting, anomaly detection, AI agents/Digital Assistant, Redwood UX).

This repo: a large library of first-class reports (financial statements incl. a new **templated financial-report builder** with IFRS layouts, ratios, ageing, registers, MRP, scorecards), dashboards/number cards, Frappe's ad-hoc Report Builder and REST API.

**Gaps:** 🔴 semantic layer / governed self-service BI (users hit SQL or Insights app); 🔴 prebuilt warehouse & cross-module KPIs; 🔴 **any** ML/AI — grep confirms zero LLM/ML libraries in the codebase; the "smartest" logic is rule-based (bank rules, scorecards, ledger health). In 2026 this is Oracle's fastest-widening lead.

### 3.19 Platform & operations

| Dimension | Oracle | ERPNext (Frappe) |
|---|---|---|
| Deployment | SaaS only (Fusion), quarterly forced updates | Self-host, Frappe Cloud, air-gapped — full freedom |
| Extensibility | VBCS/APEX/PaaS around a closed core | Open source; DocType/custom fields/server scripts modify anything |
| Integration | OIC, prebuilt adapters, Business Network | Auto-generated REST for every doctype, webhooks; fewer prebuilt adapters |
| Upgrades | Oracle-managed | Self-managed; customization discipline required |
| Scale | Very large enterprise proven | MariaDB/Postgres monolith + workers; proven mid-market, unproven at F500 transaction volumes |
| Licensing cost | High per-user SaaS | GPLv3, zero license |
| Security/compliance ops | SOC/ISO/FedRAMP programs | Depends on how you host it |

---

## 4. Where this repo is *stronger* than Oracle

Worth stating explicitly, because "gap analysis" undersells them:

1. **TCO and freedom** — no license, no per-user math, no forced quarterly updates, no lock-in; full code auditability.
2. **One data model, one UX** — CRM→quote→order→manufacture→deliver→invoice→collect in a single app; Oracle spans multiple clouds with integration seams.
3. **Customization velocity** — a custom field, script, or workflow is minutes, not a PaaS project.
4. **Included extras Oracle charges separately for** — POS, subscription billing, appointment booking, customer/supplier portals, website generators.
5. **Some genuinely novel engineering** — `bisect_accounting_statements` (binary-search ledger debugging), ledger health monitors, the PDF bank-statement bounding-box importer, the generic any-doctype SLA engine, the new pure-core scheduling engine.
6. **Fit-for-segment simplicity** — an SMB can be live in weeks; Fusion implementations are measured in quarters and consultants.

---

## 5. Prioritized improvement scope

Ordered by (impact for the users this product actually serves) × (leverage from substrates already in the repo).

### Tier 1 — high impact, buildable on existing substrates

| # | Improvement | Why / substrate |
|---|---|---|
| 1 | **Cash-flow forecasting** (forward-looking, from payment schedules, POs, payroll feeds) | AR/AP due dates, payment schedules, and bank balances all exist; only the projection layer is missing. Highest CFO-visible win. |
| 2 | **Real demand forecasting** — replace the `demand_qty = 1.0` scaffold with Holt-Winters/seasonal models + forecast-accuracy (MAPE) tracking feeding MPS | `sales_forecast`, MPS, and the MRP report are already wired together; the statistics are the missing piece. |
| 3 | **Finish the scheduling engine (phases 3–5)** — Gantt boards, drag-reschedule, WO/job-card full sync | Engine core is merged at HEAD; the differentiating UX is unbuilt. |
| 4 | **ATP/CTP promise dates on Sales Order** | Stock reservation, bin projections, and lead times exist; surfacing "earliest promise date" at order entry closes a visible Oracle gap cheaply. |
| 5 | **Financial close checklist** — a Close Task doctype orchestrating PCV, revaluation, deferred-accounting runs, recon sign-offs | All the *actions* exist as separate doctypes; only the orchestration/checklist layer is missing. |
| 6 | **SoD conflict reporting** — define conflicting role/permission pairs, report users holding both | Pure read over the existing permission model; big audit-readiness win at low cost. |
| 7 | **Shipped approval-workflow content** — standard Workflow fixtures (PO above threshold, JE approval, credit-limit release) | The Frappe Workflow engine exists; ERPNext ships zero content for it. |

### Tier 2 — significant modules, medium effort

| # | Improvement | Notes |
|---|---|---|
| 8 | **Lease accounting (IFRS 16/ASC 842)** | ROU asset + lease liability + schedule generation; reuse `asset_depreciation_schedule` patterns. Table-stakes for any audited company with leases. |
| 9 | **Revenue management (ASC 606)** | Performance obligations + SSP allocation on top of existing deferred-revenue posting machinery. |
| 10 | **WMS-lite** — bin locations, directed putaway/pick, cycle-count programs | `inventory_dimension` was built to add stock dimensions like bins; putaway rules and pick lists provide the flow skeleton. Doesn't need to be LogFire — bins + cycle counts covers most mid-market warehouses. |
| 11 | **E-invoicing/CTC framework on the EDI module** — UBL/Peppol document generation + transmission plugin interface | The `code_list`/`common_code` substrate exists and is clearly pointed at this; regulatory pressure (EU ViDA, GCC, LATAM) makes this urgent for international adoption. |
| 12 | **Supplier qualification + sourcing events** | Extend the RFQ portal into scored RFx with qualification questionnaires; the supplier scorecard engine can score responses. |
| 13 | **Consolidation eliminations** — IC elimination entries, ownership %, translation, on top of the consolidation report | Move consolidation from "report" to "process". |
| 14 | **AP invoice capture** — OCR/LLM extraction into Purchase Invoice drafts | Natural first AI feature; see #17. |
| 15 | **Project resource planning** — allocations, capacity vs. Employee/holiday calendars, utilization | Requires coordination with HRMS app for employee data. |

### Tier 3 — strategic bets

| # | Improvement | Notes |
|---|---|---|
| 16 | **Semantic analytics layer / prebuilt KPI warehouse** | Counter to OTBI/FDI; the new Report Center + Insights app are seeds. |
| 17 | **Embedded AI** — document extraction, reconciliation suggestions, anomaly detection on `ledger_health` signals, a conversational assistant over the REST API | Codebase currently has zero ML; the open data model is actually an advantage for agentic AI — every doctype already has a typed REST surface. |
| 18 | **Multi-GAAP secondary ledgers** — promote `finance_book` from depreciation-only to a posting-rule-driven parallel ledger | The GL already carries four currency layers; parallel books are the missing axis. |
| 19 | **Encumbrance accounting** for public-sector/grant users | Extends the existing three-stage budget controls into GL postings. |
| 20 | **Process manufacturing** (formulas, potency, co-product cost allocation) | Opens food/chem/pharma verticals where Oracle Process Manufacturing plays. |

### Explicit non-goals (buy/integrate instead of build)

Full TMS/GTM, global payroll, denied-party screening, and F500-scale engineered infrastructure: these are ecosystems, not features, and chasing them dilutes the product's actual advantage. Integrate best-of-breed instead.

---

## 6. Bottom line

For its target segment (SMB → mid-market, plus cost- and sovereignty-sensitive larger orgs), this repo is a legitimately complete ERP whose recent commits (finite scheduling, banking SPA, MPS/MRP, EDI substrate, standard costing) show it actively climbing toward mid-enterprise capability. Oracle's durable advantages are **statutory accounting depth (leases, ASC 606, multi-GAAP, consolidation), GRC, warehouse/logistics execution, planning science, and embedded AI** — plus the compliance/scale assurances of a managed SaaS. The Tier-1 list above is where this codebase can close the most visible gaps fastest, because in six of seven cases the substrate is already merged and only the top layer is missing.
