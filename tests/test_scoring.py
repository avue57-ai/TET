"""Scoring math tests: coverage excludes constants and not-applicable, geometric composite, provisional, guard."""

from vertex.core.scoring import Component, aggregate, merge_llm

WEIGHTS = {
    "version": "test", "composite": "geometric", "attractiveness_share": 0.45,
    "tiers": {"t1_min": 75, "t2_min": 60, "t3_min": 45, "t1_min_conf": 0.6, "t1_min_coverage": 60,
              "provisional_coverage": 60, "provisional_conf": 0.5, "band_k": 25},
}


def _c(axis, name, w, value=None, conf=None, status="missing_no_data"):
    return Component(axis, name, w, value=value, confidence=conf, status=status if value is None else "scored")


def test_geometric_prevents_untransactable_t1():
    comps = [
        _c("attractiveness", "a1", 50, 5, 0.9), _c("attractiveness", "a2", 50, 5, 0.9),
        _c("transactability", "t1", 50, 1, 0.9), _c("transactability", "t2", 50, 1, 0.9),
    ]
    r = aggregate(comps, [], WEIGHTS, "high")
    assert r.attractiveness == 100 and r.transactability == 20
    assert r.vertex_score < 50 and r.tier == "Hold"


def test_coverage_excludes_constants_and_not_applicable():
    comps = [
        Component("attractiveness", "industry_fit", 50, value=4, confidence=0.7, status="thesis_constant"),
        _c("attractiveness", "growth", 50, 4, 0.8),
        _c("transactability", "founder_ownership", 60, 5, 0.6),
        Component("transactability", "openness_evidence", 40, status="missing_not_applicable"),
    ]
    r = aggregate(comps, [], WEIGHTS, "med")
    assert r.coverage_pct == 100.0  # constants and N/A leave the denominator
    assert r.attractiveness is not None and r.transactability == 100.0


def test_missing_is_never_imputed_and_provisional_flags():
    comps = [
        _c("attractiveness", "growth", 50, 4, 0.8), _c("attractiveness", "moat", 50),
        _c("transactability", "founder_ownership", 50, 5, 0.6), _c("transactability", "size_fit", 50),
    ]
    r = aggregate(comps, [], WEIGHTS, "med")
    assert r.coverage_pct == 50.0 and r.provisional is True
    assert r.data_gaps.keys() == {"moat", "size_fit"}
    assert r.tier != "T1"


def test_hard_exclusion_wins():
    comps = [_c("attractiveness", "growth", 100, 5, 0.9), _c("transactability", "size_fit", 100, 5, 0.9)]
    r = aggregate(comps, ["institutional capital on record"], WEIGHTS, "high")
    assert r.tier == "Exclude" and r.vertex_score is not None


def test_band_widens_with_low_confidence():
    hi = aggregate([_c("attractiveness", "g", 100, 4, 0.9), _c("transactability", "s", 100, 4, 0.9)], [], WEIGHTS, "high")
    lo = aggregate([_c("attractiveness", "g", 100, 4, 0.3), _c("transactability", "s", 100, 4, 0.3)], [], WEIGHTS, "high")
    assert (hi.band_high - hi.band_low) < (lo.band_high - lo.band_low)


def test_evidence_substring_guard():
    material = "Acme provides a subscription-based compliance platform used by 300 hospitals."
    comps = [Component("attractiveness", "recurring_revenue", 12), Component("attractiveness", "moat", 8)]
    llm = {
        "recurring_revenue": {"value": 5, "confidence": 0.9, "evidence_quote": "subscription-based compliance platform"},
        "moat": {"value": 4, "confidence": 0.5, "evidence_quote": "FDA validated and SOC2 certified"},
    }
    out = merge_llm(comps, llm, material)
    rr = next(c for c in out if c.name == "recurring_revenue")
    moat = next(c for c in out if c.name == "moat")
    assert rr.status == "scored" and rr.value == 5 and rr.confidence == 0.6  # capped
    assert moat.status == "missing_no_data" and moat.value is None
