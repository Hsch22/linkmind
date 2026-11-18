-- Web articles shared as links (fetched by Core behind a host allowlist), and quoted replies in chats.

ALTER TABLE source DROP CONSTRAINT source_kind_check;
ALTER TABLE source ADD CONSTRAINT source_kind_check
    CHECK (kind IN ('text_paste', 'screenshot', 'voice', 'file', 'note', 'mixed', 'link'));
ALTER TABLE source ADD COLUMN url text;  -- canonical URL for kind = 'link'

-- WeChat shows a quoted reply as the reply bubble plus a gray quote box under it;
-- the quote belongs to that message instead of being a message of its own.
ALTER TABLE source_segment ADD COLUMN quote_text text;
