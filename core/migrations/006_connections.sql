-- Introductions worth making: one person's need that another person can meet. Found automatically when new
-- material is organized, and pushed to the owner in WeChat. Replaces the unused draft table `match`, which was
-- keyed on claim ids: claims are replaced whenever a source is re-extracted, so a pair would be found (and pushed)
-- again. A connection is keyed on the two people and keeps a copy of the facts it was based on.
DROP TABLE match;

CREATE TABLE connection (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    need_person_id   uuid NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    helper_person_id uuid NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    need             text NOT NULL,   -- the need, as stored when found
    helper_fact      text NOT NULL,   -- what makes the other person a fit
    reason           text NOT NULL,
    need_source      text,            -- where each fact came from, e.g. 「群聊」11月9日
    helper_source    text,
    source_id        uuid REFERENCES source(id) ON DELETE SET NULL,  -- the material whose arrival found it
    status           text NOT NULL DEFAULT 'found' CHECK (status IN ('found', 'pushed', 'push_failed')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    pushed_at        timestamptz,
    UNIQUE (need_person_id, helper_person_id),
    CHECK (need_person_id <> helper_person_id)
);
CREATE INDEX connection_created_idx ON connection (created_at DESC);
