You are prescreening a company for a buy-side origination engine. Decide, from the MATERIAL only, whether the company
fits the size band and the ownership rule. Do not guess: when the material is silent, answer unknown/null.

Thesis: {{ thesis_name }}
Size band: {{ employees_lo }}-{{ employees_hi }} employees; estimated revenue ${{ revenue_lo_m }}M-${{ revenue_hi_m }}M.
Ownership rule: founder, family or management owned; ANY private equity, venture capital, growth equity, search fund
or family-office sponsor investment disqualifies; subsidiaries, public companies and roll-up members are excluded;
employee-owned (ESOP) companies go on a separate track.

Company: {{ company_name }} ({{ domain }}), {{ hq }}. Inven says employees={{ employees }}, ownership_type={{ ownership_type }},
investors={{ investors }}, founded={{ founded }}.

MATERIAL:
<<<
{{ material }}
>>>

Thesis fit: the company must itself develop and sell software (or a software-enabled service with recurring revenue) in this
vertical: {{ vertical_description }}. Consultancies, training providers, staffing firms, resellers and hardware makers do not fit.

Return:
- thesis_fit: true/false/null with fit_reason (one sentence).
- size_band_ok: true/false/null (null when the material gives no headcount or revenue clue beyond Inven's own numbers).
- ownership: one of founder, family, management, esop, pe_backed, vc_backed, corporate, public, unknown.
- ownership_evidence: verbatim quote supporting the ownership answer, else null.
- institutional_capital_evidence: verbatim quote of any "backed by", "portfolio company", "investment from", "acquired by", else null.
- keep: true only if ownership is founder/family/management (or unknown with no contrary evidence) AND size is ok or unknown.
- confidence: 0-1 (never above 0.6 when the ownership answer rests on absence of evidence).
- reason: one short sentence.
