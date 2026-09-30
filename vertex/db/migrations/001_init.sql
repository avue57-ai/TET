-- Vertex Origination Engine — schema v1
-- Conventions: ids are INTEGER PRIMARY KEY; timestamps are ISO-8601 UTC text; JSON columns end in _json.
-- Unknown is NULL, never a guessed value. Provenance lives in source_records + field_provenance.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- theses
CREATE TABLE IF NOT EXISTS theses (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  version_hash TEXT NOT NULL,
  config_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- companies (system of record)
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL UNIQUE,                 -- normalized registrable domain
  name TEXT,
  name_norm TEXT,
  legal_name TEXT,
  website TEXT,
  hq_city TEXT,
  hq_state TEXT,
  hq_country TEXT,
  region TEXT,
  industry TEXT,
  sub_industry TEXT,
  vertical TEXT,                               -- thesis vertical key that surfaced it first
  description TEXT,
  products_services TEXT,
  end_markets TEXT,
  business_model TEXT,
  ownership_type TEXT CHECK (ownership_type IN ('founder','family','management','esop','pe_backed','vc_backed','corporate','public','unknown')) DEFAULT 'unknown',
  ownership_detail TEXT,
  ownership_confidence TEXT CHECK (ownership_confidence IN ('high','med','low')),
  est_revenue_low REAL,
  est_revenue_high REAL,
  revenue_basis TEXT CHECK (revenue_basis IN ('registry','public_reported','estimate','inferred','unknown')) DEFAULT 'unknown',
  revenue_fiscal_year INTEGER,
  est_ebitda REAL,
  ebitda_status TEXT CHECK (ebitda_status IN ('reported','unavailable')) DEFAULT 'unavailable',
  employee_count INTEGER,
  employee_source TEXT,
  year_founded INTEGER,
  recurring_revenue_type TEXT CHECK (recurring_revenue_type IN ('subscription','contract','reoccurring','transactional','unknown')) DEFAULT 'unknown',
  customer_concentration_status TEXT CHECK (customer_concentration_status IN ('unknown','noted','from_call')) DEFAULT 'unknown',
  customer_concentration_note TEXT,
  growth_json TEXT,
  acquisition_history_json TEXT,
  capital_raised_total REAL,
  funding_rounds INTEGER,
  last_funding_date TEXT,
  last_funding_type TEXT,
  investors_json TEXT,
  ownership_changes_json TEXT,
  founder_name TEXT,
  founder_title TEXT,
  founder_tenure_years REAL,
  founder_active INTEGER,
  linkedin_url TEXT,
  inven_url TEXT,
  apollo_org_id TEXT,
  lead_source TEXT,
  source_query_id INTEGER,
  date_discovered TEXT NOT NULL,
  last_refreshed TEXT,
  current_stage TEXT,                          -- denormalized from thesis_companies of the active thesis
  locked_fields_json TEXT,                     -- human-corrected fields that ingest must never overwrite
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_vertical ON companies(vertical);
CREATE INDEX IF NOT EXISTS idx_companies_stage ON companies(current_stage);
CREATE INDEX IF NOT EXISTS idx_companies_name_norm ON companies(name_norm);

CREATE TABLE IF NOT EXISTS company_aliases (
  id INTEGER PRIMARY KEY,
  alias_type TEXT NOT NULL CHECK (alias_type IN ('domain','name_state','linkedin','inven_id','apollo_id','lemlist_company_id')),
  alias_value TEXT NOT NULL,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(alias_type, alias_value)
);

CREATE TABLE IF NOT EXISTS source_records (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('inven','apollo_org','apollo_person','websearch','lemlist','granola','csv','human')),
  source_id TEXT,
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  batch_id INTEGER,
  UNIQUE(source, source_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_source_records_company ON source_records(company_id);

CREATE TABLE IF NOT EXISTS field_provenance (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  value_text TEXT,
  source TEXT NOT NULL,
  source_ref TEXT,
  confidence REAL,
  is_estimate INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_field_prov_company ON field_provenance(company_id, field);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  file_path TEXT,
  file_sha256 TEXT UNIQUE,
  job_id INTEGER,
  rows INTEGER DEFAULT 0,
  inserted INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0,
  skipped INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);

-- ---------------------------------------------------------------- sourcing
CREATE TABLE IF NOT EXISTS source_queries (
  id INTEGER PRIMARY KEY,
  thesis_id INTEGER NOT NULL REFERENCES theses(id),
  vertical TEXT NOT NULL,
  query_kind TEXT NOT NULL CHECK (query_kind IN ('taxonomy','product','end_market','business_model','lookalike','negative_space','semantic')),
  description TEXT NOT NULL,
  inven_search_id TEXT,
  column_selection_id TEXT,
  estimated_total INTEGER,
  interpretation_notes_json TEXT,
  halted_reason TEXT,
  rows_returned INTEGER DEFAULT 0,
  new_companies INTEGER DEFAULT 0,
  prescreen_keeper_rate REAL,
  priority REAL DEFAULT 1.0,
  status TEXT NOT NULL DEFAULT 'planned',      -- planned | built | running | done | halted | paused
  created_at TEXT NOT NULL,
  run_at TEXT
);

CREATE TABLE IF NOT EXISTS thesis_companies (
  thesis_id INTEGER NOT NULL REFERENCES theses(id),
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  source_query_id INTEGER REFERENCES source_queries(id),
  inven_relevance REAL,
  prescreen_status TEXT NOT NULL DEFAULT 'pending' CHECK (prescreen_status IN ('pending','keep','drop','unclear')),
  prescreen_evidence_json TEXT,
  track TEXT NOT NULL DEFAULT 'founder' CHECK (track IN ('founder','esop')),
  stage TEXT NOT NULL DEFAULT 'Identified',
  stage_updated_at TEXT,
  exclusion_reason TEXT,
  outreach_status TEXT,
  campaign_status TEXT,
  response_status TEXT,
  added_at TEXT NOT NULL,
  PRIMARY KEY (thesis_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_thesis_companies_stage ON thesis_companies(thesis_id, stage);

-- ---------------------------------------------------------------- contacts
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  first_name TEXT,
  last_name TEXT,
  title TEXT,
  seniority TEXT,
  role_rank INTEGER,                            -- 1 Founder-CEO ... 9 other senior; NULL = not a decision maker
  email TEXT,
  email_status TEXT CHECK (email_status IN ('verified','likely','unverified','invalid','bounced','unavailable')),
  email_confidence REAL,
  linkedin_url TEXT,
  phone TEXT,
  phone_source TEXT,
  source TEXT,
  apollo_person_id TEXT UNIQUE,
  confidence REAL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  selection_reason TEXT,
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  employment_verified_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);

-- ---------------------------------------------------------------- scoring
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  thesis_id INTEGER NOT NULL REFERENCES theses(id),
  weights_version TEXT NOT NULL,
  prompt_hash TEXT,
  attractiveness REAL,
  attractiveness_conf REAL,
  transactability REAL,
  transactability_conf REAL,
  vertex_score REAL,
  vertex_conf REAL,
  band_low REAL,
  band_high REAL,
  coverage_pct REAL,
  provisional INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL CHECK (tier IN ('T1','T2','T3','Hold','Exclude')),
  hard_exclusion_reason TEXT,
  data_gaps_json TEXT,
  scored_at TEXT NOT NULL,
  scored_by TEXT NOT NULL DEFAULT 'engine',
  human_override_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_company ON scores(company_id, scored_at);

CREATE TABLE IF NOT EXISTS score_components (
  id INTEGER PRIMARY KEY,
  score_id INTEGER NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  axis TEXT NOT NULL CHECK (axis IN ('attractiveness','transactability')),
  component TEXT NOT NULL,
  weight REAL NOT NULL,
  value REAL,                                   -- 0..5 or NULL
  confidence REAL,
  evidence_text TEXT,
  evidence_source_ref TEXT,
  status TEXT NOT NULL CHECK (status IN ('scored','missing_no_data','missing_not_applicable','thesis_constant')),
  next_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_score_components_score ON score_components(score_id);

-- ---------------------------------------------------------------- research signals
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  hook_type TEXT NOT NULL,
  text TEXT NOT NULL,
  evidence_quote TEXT,
  evidence_source TEXT NOT NULL,                -- inven_description | inven_keywords | inven_news | apollo_org | websearch_summary | granola | human
  evidence_url TEXT,
  confidence REAL,
  safe_to_cite INTEGER NOT NULL DEFAULT 0,
  banned_theme INTEGER NOT NULL DEFAULT 0,
  freshness TEXT CHECK (freshness IN ('fresh','recent','aging','stale','undated')),
  observed_date TEXT,
  captured_at TEXT NOT NULL,
  used_in_outreach INTEGER NOT NULL DEFAULT 0,
  human_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_company ON signals(company_id);

-- ---------------------------------------------------------------- outreach copy
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  campaign_id INTEGER,
  sequence_version TEXT NOT NULL,
  step_key TEXT NOT NULL,                       -- li_note | li_msg | email1 .. email5 | email1_subject_b
  channel TEXT NOT NULL CHECK (channel IN ('email','linkedin')),
  variant_key TEXT,
  hook_signal_id INTEGER REFERENCES signals(id),
  hook_type TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  prompt_version TEXT,
  lint_flags_json TEXT,
  critic_score REAL,
  critic_flags_json TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','edited','rejected','needs_edit')),
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id, sequence_version, step_key);

-- ---------------------------------------------------------------- campaigns & engagement
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  lemlist_campaign_id TEXT UNIQUE,
  name TEXT NOT NULL,
  thesis_id INTEGER REFERENCES theses(id),
  wave INTEGER NOT NULL DEFAULT 1,
  arm TEXT NOT NULL CHECK (arm IN ('linkedin_led','email_first')),
  sequence_version TEXT NOT NULL,
  senders_json TEXT,
  lemlist_state TEXT,
  build_path TEXT,                              -- api | duplicate
  readiness_json TEXT,
  created_at TEXT NOT NULL,
  launched_at TEXT,
  launched_by TEXT
);

CREATE TABLE IF NOT EXISTS enrollments (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER NOT NULL REFERENCES contacts(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  lemlist_lead_id TEXT,
  lemlist_contact_id TEXT,
  variant_key TEXT,
  hook_type TEXT,
  channel_order TEXT,
  channel_scope TEXT NOT NULL DEFAULT 'full' CHECK (channel_scope IN ('full','linkedin_only')),
  tier_at_enroll TEXT,
  score_id INTEGER REFERENCES scores(id),
  decision_id INTEGER NOT NULL,                 -- review_decisions.id that authorized this enrollment
  gate_results_json TEXT,
  pushed_at TEXT,
  push_outcome TEXT,
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','pushed','active','paused','stopped','finished','bounced','failed')),
  paused_at TEXT,
  pause_reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(contact_id, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_enrollments_company ON enrollments(company_id);

CREATE TABLE IF NOT EXISTS engagement_events (
  id INTEGER PRIMARY KEY,
  enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE CASCADE,
  lemlist_activity_id TEXT NOT NULL UNIQUE,
  lemlist_lead_id TEXT,
  campaign_lemlist_id TEXT,
  event_type TEXT NOT NULL,
  step_index INTEGER,
  channel TEXT,
  occurred_at TEXT NOT NULL,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_enrollment ON engagement_events(enrollment_id, occurred_at);

CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY,
  enrollment_id INTEGER REFERENCES enrollments(id) ON DELETE SET NULL,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  lemlist_contact_id TEXT,
  lemlist_message_id TEXT UNIQUE,
  channel TEXT,
  received_at TEXT NOT NULL,
  text TEXT,
  ai_lead_interest INTEGER,
  predicted_class TEXT,
  predicted_confidence REAL,
  extracted_json TEXT,
  final_class TEXT,
  classified_by TEXT CHECK (classified_by IN ('rule','llm','human')),
  rationale TEXT,
  suggested_reply TEXT,
  auto_actions_json TEXT,
  human_reviewed INTEGER NOT NULL DEFAULT 0,
  reviewed_at TEXT
);

-- ---------------------------------------------------------------- pipeline, tasks, review
CREATE TABLE IF NOT EXISTS stage_transitions (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  thesis_id INTEGER NOT NULL REFERENCES theses(id),
  from_stage TEXT,
  to_stage TEXT NOT NULL,
  reason TEXT,
  actor TEXT NOT NULL CHECK (actor IN ('system','llm','human')),
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  type TEXT NOT NULL,
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
  created_from TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS review_items (
  id INTEGER PRIMARY KEY,
  item_type TEXT NOT NULL CHECK (item_type IN ('reply_action','enroll','accept_target','contact_choice','score_dispute','exception','weights_proposal','prescreen_gate','message')),
  ref_table TEXT,
  ref_id INTEGER,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  priority INTEGER NOT NULL DEFAULT 3,
  short_id TEXT,
  payload_json TEXT,
  review_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','decided','expired')),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_items_status ON review_items(status, review_date);

CREATE TABLE IF NOT EXISTS review_decisions (
  id INTEGER PRIMARY KEY,
  review_item_id INTEGER NOT NULL REFERENCES review_items(id),
  decision TEXT NOT NULL CHECK (decision IN ('approve','reject','hold','edit','replace_contact','snooze','reclassify')),
  reason_code TEXT,
  payload_json TEXT,
  decided_by TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('page','chat','cli')),
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('domain','email','linkedin')),
  value TEXT NOT NULL,
  reason TEXT NOT NULL,
  source TEXT,
  expires_at TEXT,
  hold_for_human INTEGER NOT NULL DEFAULT 0,    -- existing_relationship: held for a human decision rather than auto-skipped
  added_at TEXT NOT NULL,
  UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY,
  granola_meeting_id TEXT UNIQUE,
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  matched_by TEXT,
  held_at TEXT,
  outcome TEXT,
  summary TEXT,
  created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- experiments & feedback
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  variable TEXT NOT NULL,
  arms_json TEXT NOT NULL,
  campaign_ids_json TEXT,
  started_at TEXT,
  ended_at TEXT,
  pooled_results_json TEXT,
  conclusion TEXT,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS weights_versions (
  version TEXT PRIMARY KEY,
  yaml_text TEXT NOT NULL,
  evidence_json TEXT,
  backtest_json TEXT,
  proposed_at TEXT NOT NULL,
  approved_by TEXT,
  applied_at TEXT,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','active','retired'))
);

-- ---------------------------------------------------------------- bridge, llm, runs
CREATE TABLE IF NOT EXISTS bridge_jobs (
  id INTEGER PRIMARY KEY,
  connector TEXT NOT NULL,                      -- inven | apollo | lemlist | websearch | granola
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  purpose TEXT NOT NULL,                        -- router key
  batch_key TEXT,
  context_json TEXT,                            -- ids the ingest needs (thesis_id, source_query_id, company ids...)
  est_credits REAL NOT NULL DEFAULT 0,
  credit_type TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  completed_at TEXT,
  result_file TEXT,
  result_bytes INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_bridge_jobs_status ON bridge_jobs(status, connector);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  prompt_name TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  model TEXT,
  output_json TEXT,
  cost_usd REAL,
  duration_ms INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(prompt_name, prompt_version, input_hash)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  job TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed')),
  summary_json TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id),
  provider TEXT NOT NULL,
  credit_type TEXT NOT NULL,
  amount REAL NOT NULL,
  note TEXT,
  at TEXT NOT NULL
);

-- ---------------------------------------------------------------- views
CREATE VIEW IF NOT EXISTS v_latest_score AS
SELECT s.* FROM scores s
JOIN (SELECT company_id, thesis_id, MAX(id) AS max_id FROM scores GROUP BY company_id, thesis_id) m
  ON m.max_id = s.id;

CREATE VIEW IF NOT EXISTS v_funnel AS
SELECT t.slug AS thesis, tc.stage, COUNT(*) AS companies
FROM thesis_companies tc JOIN theses t ON t.id = tc.thesis_id
GROUP BY t.slug, tc.stage;

CREATE VIEW IF NOT EXISTS v_campaign_perf AS
SELECT c.id AS campaign_id, c.name, c.arm,
  COUNT(DISTINCT e.id) AS enrolled,
  COUNT(DISTINCT CASE WHEN ev.event_type IN ('emailsSent','linkedinInviteDone','linkedinSent') THEN e.id END) AS contacted,
  COUNT(DISTINCT CASE WHEN ev.event_type IN ('emailsReplied','linkedinReplied') THEN e.id END) AS replied,
  COUNT(DISTINCT CASE WHEN r.final_class IN ('Interested','Open to conversation','Referral','Follow up in X months') THEN e.id END) AS positive
FROM campaigns c
LEFT JOIN enrollments e ON e.campaign_id = c.id
LEFT JOIN engagement_events ev ON ev.enrollment_id = e.id
LEFT JOIN replies r ON r.enrollment_id = e.id
GROUP BY c.id, c.name, c.arm;

CREATE VIEW IF NOT EXISTS v_subject_perf AS
SELECT m.subject,
  COUNT(DISTINCT e.id) AS sends,
  COUNT(DISTINCT CASE WHEN ev.event_type = 'emailsReplied' THEN e.id END) AS replies,
  COUNT(DISTINCT CASE WHEN r.final_class IN ('Interested','Open to conversation','Referral','Follow up in X months') THEN e.id END) AS positives,
  CASE WHEN COUNT(DISTINCT e.id) < 30 THEN 1 ELSE 0 END AS directional
FROM messages m
JOIN enrollments e ON e.contact_id = m.contact_id AND e.campaign_id = m.campaign_id
LEFT JOIN engagement_events ev ON ev.enrollment_id = e.id
LEFT JOIN replies r ON r.enrollment_id = e.id
WHERE m.step_key = 'email1' AND m.status IN ('approved','edited')
GROUP BY m.subject;
