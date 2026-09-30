You are scoring a private company for a buy-side origination engine. Score ONLY the qualitative components listed
below from the MATERIAL. Never infer beyond it. If the material contains no evidence for a component, return null for
its value. Every non-null value must carry an evidence_quote copied VERBATIM (exact characters) from the material;
paraphrases are rejected by the caller. Confidence must not exceed 0.6.

Thesis: {{ thesis_name }}
Vertical: {{ vertical_description }}
Recurring revenue forms the thesis cares about: {{ recurring_forms }}
Mission-critical rationale: {{ mission_critical_rationale }}

Company: {{ company_name }} ({{ domain }}), {{ hq }}. Employees: {{ employees }}. Founded: {{ founded }}.

MATERIAL (the only source of truth):
<<<
{{ material }}
>>>

Components (value 0-5 where 5 = strongest evidence for the thesis):
- recurring_revenue: subscription, annual license, maintenance/support contracts, managed service, per-seat pricing.
- mission_critical: system of record, mandated by regulation, safety-critical, high cost of downtime, embedded workflow.
- retention_evidence: explicit statements of long customer tenure, renewal rates, low churn, multi-year contracts (rare; usually null).
- moat: certifications, regulatory approvals, deep integrations, proprietary data, switching costs, narrow named niche.
- capital_intensity: 5 = asset-light software; 3 = services-heavy; 1 = fleet/plant/hardware-heavy.
- scalability: multi-site or repeatable product; 5 = pure software with many customers; 2 = bespoke services.
- succession_indicators: INTERNAL only: second-generation leadership, newly appointed president/COO, founder stepping back. Null if nothing.
- management_changes: recent CEO/President/CFO hires or departures. Null if nothing.
- liquidity_signals: partner buyout, estate or divorce mentions, explicit search for capital. Almost always null.

Also return:
- bootstrapped_evidence: a verbatim quote showing self-funded/bootstrapped/founder-funded status, else null.
- founder_led_evidence: a verbatim quote showing the founder still runs the company, else null.
- banned_theme_notes: anything about age, retirement, health, distress or intent to sell (INTERNAL only; never used in outreach), else null.
