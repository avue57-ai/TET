-- Campaign build state (root/branch sequence ids, step ids) for the API build path.
ALTER TABLE campaigns ADD COLUMN build_state_json TEXT;
