Classify one reply to buy-side origination outreach. Vertex Equity (Asean Vue) wrote to a founder or owner on behalf of an
anonymous long-term buyer. Decide what the reply means and extract anything actionable. Quote the reply in `rationale`.

Contact: {{ contact_name }}, {{ contact_title }} at {{ company_name }} ({{ vertical }}). Channel: {{ channel }}.
Lemlist's own interest score (0-3, may be missing): {{ ai_lead_interest }}

The message they are answering:
<<<
{{ our_message }}
>>>

Their reply:
<<<
{{ reply_text }}
>>>
{% if thread %}
Earlier thread (oldest first):
<<<
{{ thread }}
>>>
{% endif %}

Classes (pick exactly one):
- Interested: wants to talk, asks for a call, asks who the buyer is with clear openness.
- Open to conversation: curious but hedged ("send more info", "what are you thinking", "maybe later this quarter").
- Not now: not the right time; no hostility; may revisit. Set follow_up_months if they name a time.
- Follow up in X months: explicitly asks to reconnect at a time; set follow_up_months.
- Not interested: clear no, without hostility or with it.
- Already sold / PE-backed: says the company was sold, has investors, is part of a group, or is otherwise not independently owned. Put the fact in ownership_fact.
- Wrong person: says someone else handles this; put the name/title/hint in referral if given.
- Referral: points us to another company or person as a better fit (referral fields).
- Remove me: asks to stop, unsubscribe, or never contact again.
- OOO: automatic out-of-office; set return_date if stated.
- Bounce: delivery failure text.
- Unclear: cannot tell (empty, signature only, off-topic).

Confidence 0-1 is your certainty in the class. Be conservative: an unclear "thanks" is Unclear, not Interested.
