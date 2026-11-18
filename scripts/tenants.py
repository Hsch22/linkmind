"""Testers on this Mac: each person besides the owner gets their own WeChat bot, OpenClaw agent, LinkMind Core
service (own port) and database. Used by scripts/dev.sh; standard library only.

Registry: $LINKMIND_HOME/tenants/<name>/tenant.env with PORT=... and, once their bot is logged in, ACCOUNT=...

  tenants.py list                  # tester names
  tenants.py get NAME KEY          # one registry value
  tenants.py next-port             # first free Core port after the owner's 8765
  tenants.py accounts              # every WeChat bot account logged in on this Mac
  tenants.py owner-accounts        # accounts no tester holds: the owner's bot (more than one only mid-login)
  tenants.py main-account          # the owner's bot: the one account no tester has (empty if not exactly one)
  tenants.py new-account FILE      # the account logged in since FILE was written by `accounts`
  tenants.py openclaw-patch        # agents, bindings and MCP servers for OpenClaw (JSON)
  tenants.py welcome NAME|main     # send the usage note to that bot's user in WeChat (exit 1 if not sent)
  tenants.py welcome-text NAME     # the usage note: openclaw/welcome.txt, plus where a tester's data lives
"""

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

HOME = Path(os.environ.get("LINKMIND_HOME", Path.home() / ".linkmind")).expanduser()
TENANTS = HOME / "tenants"
WEIXIN = Path.home() / ".openclaw" / "openclaw-weixin"
WELCOME = Path(__file__).resolve().parents[1] / "openclaw" / "welcome.txt"
ID = re.compile(r"[A-Za-z0-9_.@-]+")  # WeChat bot account and user ids
# Testers hosted on the owner's computer are told where their data lives.
TESTER_NOTE = "你的资料单独存放在邀请你的人的电脑上，不想用了告诉邀请人，会全部删除。"
NAME = re.compile(r"[a-z0-9]{1,16}")
OWNER_PORT = 8765


def names() -> list[str]:
    if not TENANTS.is_dir():
        return []
    return sorted(p.name for p in TENANTS.iterdir() if NAME.fullmatch(p.name) and (p / "tenant.env").is_file())


def registry(name: str) -> dict[str, str]:
    out = {}
    for line in (TENANTS / name / "tenant.env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def accounts() -> list[str]:
    try:
        return [a for a in json.loads((WEIXIN / "accounts.json").read_text()) if isinstance(a, str)]
    except (OSError, ValueError):
        return []


def owner_accounts() -> list[str]:
    taken = {registry(n).get("ACCOUNT") for n in names()}
    return [a for a in accounts() if a not in taken]


def main_account() -> str:
    rest = owner_accounts()
    return rest[0] if len(rest) == 1 else ""


def mcp_server(port: int) -> dict:
    return {"url": f"http://127.0.0.1:{port}/mcp", "transport": "streamable-http", "requestTimeoutMs": 90000}


GUEST = "guest"  # agent for a bot that is logged in but not yet bound to its person (during add-tester)


def openclaw_patch() -> dict:
    """Each tester's agent may only call its own Core (`lm_<name>__*`); the owner's agent only `linkmind__*`
    (openclaw/config.json5). Bindings route each bot to its person's agent. Any other bot -- one a tester has just
    logged in, before it is bound -- goes to the guest agent, which has no tools, so OpenClaw refuses to run it and
    the message gets no reply. The explicit channel-wide rule also stops OpenClaw from adding its own, which would
    route such a bot to the owner's agent (its migration to multi-agent routing does that)."""
    entries = {
        GUEST: {
            "name": GUEST,
            "workspace": str(Path.home() / ".openclaw" / f"workspace-{GUEST}"),
            "tools": {"deny": ["*"]},
        }
    }
    bindings, servers = [], {"linkmind": mcp_server(OWNER_PORT)}
    if owner := main_account():
        bindings.append({"agentId": "main", "match": {"channel": "openclaw-weixin", "accountId": owner}})
    for n in names():
        reg = registry(n)
        entries[n] = {
            "name": n,
            "workspace": str(Path.home() / ".openclaw" / f"workspace-{n}"),
            "tools": {"allow": [f"lm_{n}__*"]},
        }
        servers[f"lm_{n}"] = mcp_server(int(reg["PORT"]))
        if reg.get("ACCOUNT"):
            bindings.append({"agentId": n, "match": {"channel": "openclaw-weixin", "accountId": reg["ACCOUNT"]}})
    bindings.append({"agentId": GUEST, "match": {"channel": "openclaw-weixin", "accountId": "*"}})
    return {"agents": {"entries": entries}, "bindings": bindings, "mcp": {"servers": servers}}


def welcome_text(name: str) -> str:
    text = WELCOME.read_text().strip()
    return text if name == "main" else f"{text}\n\n{TESTER_NOTE}"


def welcome(name: str) -> bool:
    """Send the usage note to a new user as soon as their bot is bound: a tester, or the owner ("main") after the
    first login on a new computer. It goes only to the WeChat user who logged that bot in (the one person allowed
    to message it). Sent inside the gateway like LinkMind's other pushes (core/linkmind/connect.py) and mirrored
    into their chat with their agent, so it knows what it told them."""
    account = main_account() if name == "main" else registry(name).get("ACCOUNT", "")
    if not ID.fullmatch(account):
        return False
    try:
        user = json.loads((WEIXIN / "accounts" / f"{account}.json").read_text())["userId"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(user, str) or not ID.fullmatch(user):
        return False
    params = {
        "to": user, "message": welcome_text(name), "channel": "openclaw-weixin", "accountId": account,
        "sessionKey": f"agent:{name}:openclaw-weixin:direct:{user.lower()}", "idempotencyKey": str(uuid.uuid4()),
    }
    try:
        r = subprocess.run(
            ["openclaw", "gateway", "call", "send", "--json", "--timeout", "30000",
             "--params", json.dumps(params, ensure_ascii=False)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"welcome not sent: {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"welcome not sent: {(r.stderr or r.stdout)[-500:]}", file=sys.stderr)
    return r.returncode == 0


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("list", [])
    if cmd == "list":
        print("\n".join(names()))
    elif cmd == "get":
        print(registry(args[0]).get(args[1], ""))
    elif cmd == "next-port":
        used = {int(registry(n)["PORT"]) for n in names() if registry(n).get("PORT")}
        print(next(p for p in range(OWNER_PORT + 1, OWNER_PORT + 200) if p not in used))
    elif cmd == "accounts":
        print("\n".join(accounts()))
    elif cmd == "owner-accounts":
        print("\n".join(owner_accounts()))
    elif cmd == "main-account":
        print(main_account())
    elif cmd == "new-account":
        before = set(Path(args[0]).read_text().split())
        new = [a for a in accounts() if a not in before]
        print(new[0] if len(new) == 1 else "")
    elif cmd == "openclaw-patch":
        print(json.dumps(openclaw_patch(), ensure_ascii=False, indent=2))
    elif cmd == "welcome-text":
        print(welcome_text(args[0]))
    elif cmd == "welcome":
        return 0 if welcome(args[0]) else 1
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
