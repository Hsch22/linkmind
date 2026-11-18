-- The owner's names in WeChat. The WeChat nickname comes from LINKMIND_OWNER_NAMES; each group can have its
-- own "我在本群的昵称", which is learned (someone quotes the owner's message) or told to the bot.
CREATE TABLE owner_alias (
    alias       text PRIMARY KEY,
    chat_title  text,      -- the chat it was seen in or set for; NULL = not tied to one chat
    origin      text NOT NULL CHECK (origin IN ('user', 'learned')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
