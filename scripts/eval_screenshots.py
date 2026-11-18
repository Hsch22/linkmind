"""Phase 0: how well does screenshot transcription attribute speakers?

Put real WeChat screenshots in a folder. Optional ground truth: next to `chat1.png`, a
`chat1.speakers` file with one line per message, each `me`, `them`, `other` or `system`.

    scripts/dev.sh eval ~/Desktop/screens
    scripts/dev.sh eval ~/Desktop/screens --model MoonshotAi/Kimi-K3.1-pro

Writes one JSON transcript per image into ~/.linkmind/eval/ and prints speaker accuracy
for images that have ground truth. Images are transcribed one by one (no database writes).
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from linkmind.config import LINKMIND_HOME  # noqa: E402
from linkmind.vision import transcribe_screenshots  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def main(folder: str, model: str | None) -> None:
    src = Path(folder)
    out = LINKMIND_HOME / "eval"
    out.mkdir(parents=True, exist_ok=True)
    images = sorted(p for p in src.iterdir() if p.suffix.lower() in IMAGE_EXT)
    if not images:
        sys.exit(f"no images in {src}")

    total = correct = 0
    for img in images:
        t0 = time.monotonic()
        transcript, meta = transcribe_screenshots([img], model=model)
        took = time.monotonic() - t0
        (out / f"{img.stem}.{meta['model'].replace('/', '_')}.json").write_text(
            json.dumps({"transcript": transcript.model_dump(), "llm": meta}, ensure_ascii=False, indent=2)
        )
        got = [s.speaker for s in transcript.segments]
        line = f"{img.name}: {len(got)} msgs, title={transcript.chat_title!r}, {took:.1f}s"
        truth_file = img.with_suffix(".speakers")
        if truth_file.exists():
            truth = [t.strip() for t in truth_file.read_text().splitlines() if t.strip()]
            hits = sum(a == b for a, b in zip(got, truth))
            total += len(truth)
            correct += hits
            line += f", speakers {hits}/{len(truth)}" + ("" if len(got) == len(truth) else f" (count {len(got)} vs {len(truth)})")
        print(line)
        for s in transcript.segments:
            print(f"    {s.speaker:<7} {s.text[:60]}")
    if total:
        print(f"\nspeaker accuracy: {correct}/{total} = {correct / total:.1%}")
    print(f"transcripts written to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--model", help="override LINKMIND_MODEL for this run")
    args = ap.parse_args()
    main(args.folder, args.model)
