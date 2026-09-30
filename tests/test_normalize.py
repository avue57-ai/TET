"""Tests for identifier normalization."""

from vertex.core.normalize import (
    name_state_key,
    normalize_domain,
    normalize_email,
    normalize_linkedin,
    normalize_name,
    normalize_state,
)


def test_domain_strips_scheme_www_path_and_port():
    assert normalize_domain("https://WWW.Example.com:443/about?x=1") == "example.com"
    assert normalize_domain("www.example.com/") == "example.com"
    assert normalize_domain("Example.COM") == "example.com"


def test_domain_keeps_registrable_suffix_and_drops_subdomains():
    assert normalize_domain("app.sub.example.co.uk") == "example.co.uk"
    assert normalize_domain("careers.acme.io") == "acme.io"


def test_domain_rejects_garbage():
    assert normalize_domain("") is None
    assert normalize_domain(None) is None
    assert normalize_domain("not a domain") is None
    assert normalize_domain("someone@example.com") is None
    assert normalize_domain("localhost") is None


def test_domain_idna():
    assert normalize_domain("https://bücher.de") == "xn--bcher-kva.de"


def test_name_normalization_drops_legal_suffixes_and_punctuation():
    assert normalize_name("Acme Compliance Software, Inc.") == "acme compliance software"
    assert normalize_name("The Smith & Sons Holdings LLC") == "smith and sons"
    assert normalize_name("Résumé Tech Corp") == "resume tech"
    assert normalize_name("") is None


def test_state_codes():
    assert normalize_state("Michigan") == "MI"
    assert normalize_state("mi") == "MI"
    assert normalize_state("Ontario") is None


def test_linkedin_canonical():
    assert normalize_linkedin("linkedin.com/company/Acme-Co/") == "https://www.linkedin.com/company/acme-co"
    assert normalize_linkedin("https://www.linkedin.com/in/JaneDoe?trk=x") == "https://www.linkedin.com/in/janedoe"
    assert normalize_linkedin("https://example.com/in/jane") is None


def test_email_and_name_state_key():
    assert normalize_email("  Jane@Example.COM ") == "jane@example.com"
    assert normalize_email("nope") is None
    assert name_state_key("Acme Inc", "Ohio") == "acme|OH"
    assert name_state_key("Acme Inc", None) == "acme|"
