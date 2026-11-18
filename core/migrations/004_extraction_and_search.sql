-- Facts extracted from a source point back at it, so re-extracting a source (e.g. after more screenshots
-- joined its batch) replaces exactly its own facts. Trigram indexes serve substring search (works for Chinese).

ALTER TABLE claim ADD COLUMN source_id uuid REFERENCES source(id) ON DELETE CASCADE;
CREATE INDEX claim_source ON claim (source_id);
CREATE INDEX claim_content_trgm ON claim USING gin (content gin_trgm_ops);
CREATE INDEX segment_text_trgm ON source_segment USING gin (text gin_trgm_ops);
