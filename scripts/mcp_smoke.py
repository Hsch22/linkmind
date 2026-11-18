"""Smoke-test a running LinkMind MCP server.

    scripts/dev.sh smoke                          # status + tool list + recent sources
    scripts/dev.sh smoke --dictation "..."        # ingest a dictation note (no LLM call)
    scripts/dev.sh smoke --images a.png b.png     # ingest screenshots (calls the LLM)
    scripts/dev.sh smoke --link "https://mp.weixin.qq.com/s/..."   # fetch + store an article (no LLM call)
"""

import argparse
import asyncio
import json

from mcp import Client


def show(title: str, result) -> None:
    print(f"== {title}")
    if result.is_error:
        print("ERROR:", [getattr(c, "text", c) for c in result.content])
        return
    data = result.structured_content
    if data is None:
        data = [getattr(c, "text", c) for c in result.content]
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    ap.add_argument("--dictation")
    ap.add_argument("--text", help="pasted chat text")
    ap.add_argument("--images", nargs="*")
    ap.add_argument("--link", help="message text containing one article URL")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    async with Client(args.url) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])
        show("linkmind_status", await client.call_tool("linkmind_status", {}))
        if args.dictation:
            show("ingest dictation", await client.call_tool(
                "ingest_material", {"text": args.dictation, "material_kind": "dictation", "user_note": args.note}))
        if args.text:
            show("ingest chat_text", await client.call_tool(
                "ingest_material", {"text": args.text, "material_kind": "chat_text", "user_note": args.note}))
        if args.images:
            show("ingest screenshots", await client.call_tool(
                "ingest_material", {"media_paths": args.images, "material_kind": "screenshots", "user_note": args.note},
                read_timeout_seconds=120))
        if args.link:
            show("ingest link", await client.call_tool(
                "ingest_material", {"text": args.link, "material_kind": "link", "user_note": args.note}))
        show("list_recent_sources", await client.call_tool("list_recent_sources", {"limit": 3}))


if __name__ == "__main__":
    asyncio.run(main())
