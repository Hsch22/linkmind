-- Consecutive screenshots of one chat are merged into the first source of the batch. The later source
-- keeps its raw image (still listed in the batch's media_paths) and points at the batch it joined.

ALTER TABLE source DROP CONSTRAINT source_status_check;
ALTER TABLE source ADD CONSTRAINT source_status_check
    CHECK (status IN ('received', 'transcribing', 'transcribed', 'merged', 'resolved', 'extracted', 'failed'));
ALTER TABLE source ADD COLUMN merged_into uuid REFERENCES source(id) ON DELETE CASCADE;
CREATE INDEX source_kind_received ON source (kind, received_at DESC);
