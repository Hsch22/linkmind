-- LinkMind core schema v1.
-- Principle: the smallest unit of memory is a CLAIM backed by EVIDENCE (source segments).
-- Claims are append-only: an update inserts a new claim and marks the old one superseded.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- People -------------------------------------------------------------------

CREATE TABLE person (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name  text NOT NULL,
    is_self       boolean NOT NULL DEFAULT false,   -- the user themself (owner of resources)
    organization  text,
    title         text,
    met_via       text,                              -- 认识渠道
    summary       text,                              -- derived from active claims, never a source of truth
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX person_single_self ON person (is_self) WHERE is_self;

CREATE TABLE person_alias (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id  uuid NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    alias      text NOT NULL,
    kind       text NOT NULL DEFAULT 'other'
               CHECK (kind IN ('wechat_remark', 'wechat_nickname', 'real_name', 'english_name', 'other')),
    UNIQUE (person_id, alias)
);
CREATE INDEX person_alias_trgm ON person_alias USING gin (alias gin_trgm_ops);

-- Raw material ---------------------------------------------------------------

CREATE TABLE source (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL
                    CHECK (kind IN ('text_paste', 'screenshot', 'voice', 'file', 'note', 'mixed')),
    channel         text NOT NULL DEFAULT 'wechat',
    person_id       uuid REFERENCES person(id) ON DELETE CASCADE,  -- resolved counterpart
    raw_text        text,
    media_paths     text[] NOT NULL DEFAULT '{}',   -- copies under data/raw/<source_id>/
    user_note       text,
    content_sha256  text NOT NULL,
    status          text NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received', 'transcribing', 'transcribed', 'resolved',
                                      'extracted', 'failed')),
    transcript_meta jsonb NOT NULL DEFAULT '{}',    -- chat_title, is_group, model, latency_ms, notes
    error           text,
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX source_received_at ON source (received_at DESC);
CREATE INDEX source_sha ON source (content_sha256);

CREATE TABLE source_segment (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    seq           int  NOT NULL,
    speaker       text NOT NULL CHECK (speaker IN ('me', 'them', 'other', 'system', 'unknown')),
    speaker_name  text,
    time_text     text,          -- timestamp label exactly as shown ("昨天 21:03")
    sent_at       timestamptz,   -- resolved timestamp when derivable
    content_kind  text NOT NULL DEFAULT 'text',
    text          text NOT NULL,
    UNIQUE (source_id, seq)
);

-- Claims & evidence --------------------------------------------------------------

CREATE TABLE claim (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id      uuid NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    kind           text NOT NULL
                   CHECK (kind IN ('profile', 'need', 'offer', 'commitment', 'event', 'opinion', 'relation')),
    content        text NOT NULL,
    attrs          jsonb NOT NULL DEFAULT '{}',   -- topic, urgency, share_scope, counterpart ...
    epistemic      text NOT NULL
                   CHECK (epistemic IN ('stated_by_them', 'stated_by_me', 'inferred')),
    confidence     real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    valid_from     date,
    valid_to       date,
    status         text NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'superseded', 'resolved', 'expired', 'rejected')),
    superseded_by  uuid REFERENCES claim(id),
    review         text NOT NULL DEFAULT 'pending'
                   CHECK (review IN ('pending', 'confirmed', 'corrected')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX claim_person_kind ON claim (person_id, kind) WHERE status = 'active';

CREATE TABLE claim_evidence (
    claim_id    uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    segment_id  uuid NOT NULL REFERENCES source_segment(id) ON DELETE CASCADE,
    quote       text NOT NULL,   -- verbatim span, verified by code to exist in the segment
    PRIMARY KEY (claim_id, segment_id)
);

-- The user's own resources ------------------------------------------------------

CREATE TABLE resource (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL
                  CHECK (kind IN ('project', 'material', 'capability', 'opportunity', 'event', 'other')),
    title         text NOT NULL,
    description   text,
    availability  text NOT NULL DEFAULT 'available'
                  CHECK (availability IN ('available', 'limited', 'unavailable')),
    share_scope   text NOT NULL DEFAULT 'ask_first'
                  CHECK (share_scope IN ('private', 'ask_first', 'shareable')),
    attrs         jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Matches & tasks ----------------------------------------------------------------

CREATE TABLE match (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    need_claim_id   uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    offer_claim_id  uuid REFERENCES claim(id) ON DELETE CASCADE,
    resource_id     uuid REFERENCES resource(id) ON DELETE CASCADE,
    scores          jsonb NOT NULL,  -- semantic, feasibility, reachability, timeliness, mutual_value
    total_score     real NOT NULL,
    rationale       text NOT NULL,
    constraints     text,
    next_step       text,
    status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'accepted', 'intro_drafted', 'done', 'dismissed')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (offer_claim_id IS NOT NULL OR resource_id IS NOT NULL)
);

CREATE TABLE task (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id   uuid REFERENCES person(id) ON DELETE CASCADE,
    claim_id    uuid REFERENCES claim(id) ON DELETE SET NULL,
    owner       text NOT NULL CHECK (owner IN ('me', 'them')),
    title       text NOT NULL,
    due_date    date,
    priority    smallint NOT NULL DEFAULT 2,
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Retrieval ---------------------------------------------------------------------

CREATE TABLE embedding (
    owner_kind  text NOT NULL CHECK (owner_kind IN ('claim', 'resource', 'person')),
    owner_id    uuid NOT NULL,
    model       text NOT NULL,
    vec         vector(1024) NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, model)
);
CREATE INDEX embedding_hnsw ON embedding USING hnsw (vec vector_cosine_ops);

-- Audit & probes -----------------------------------------------------------------

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL CHECK (actor IN ('system', 'user', 'llm')),
    op         text NOT NULL,
    entity     text NOT NULL,
    entity_id  uuid,
    before     jsonb,
    after      jsonb,
    source_id  uuid
);

-- Phase 0: every MCP call is recorded verbatim so we can see what WeChat/OpenClaw actually deliver.
CREATE TABLE inbound_event (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    tool     text NOT NULL,
    payload  jsonb NOT NULL
);
