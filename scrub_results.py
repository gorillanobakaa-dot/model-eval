#!/usr/bin/env python3
"""Remove account identifiers and credentials from results before publishing.

Provider error bodies are stored verbatim in result files so that failures stay
diagnosable. That is the right default for local use and the wrong one for a
public repository: an NVIDIA 404 echoes the account id, and a provider that ever
echoed part of a key would have written it to disk permanently.

Run this before publishing or sharing a results directory. It rewrites files in
place and reports exactly what it changed, so a silent no-op cannot be mistaken
for a clean bill of health.

    ./scrub_results.py --check     # report only, change nothing
    ./scrub_results.py             # rewrite in place
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GORILLA_CONFIG = Path.home() / ".config" / "gorilla-opencode" / "config.json"

# Identifier shapes that providers echo back in error bodies.
#
# Every pattern must refuse to match its own replacement. Without the
# `(?!<redacted)` guard, "for account '<redacted-account-id>'" still satisfies
# "for account '[^']+'", so each run re-redacts clean files and --check reports
# hits forever. Since --check is meant to gate publishing, a checker that can
# never reach zero is worse than no checker: it trains you to ignore it.
PATTERNS = [
    (re.compile(r"(for account ')(?!<redacted)[^']+(')"), r"\1<redacted-account-id>\2"),
    (re.compile(r"(\"account(?:_id)?\"\s*:\s*\")(?!<redacted)[^\"]+(\")"), r"\1<redacted>\2"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})\b"), "<redacted-key>"),
    (re.compile(r"\b(nvapi-[A-Za-z0-9_\-]{16,})\b"), "<redacted-key>"),
]


def live_keys() -> list[str]:
    """Any credential currently configured, so it can be matched literally."""
    keys = []
    if GORILLA_CONFIG.exists():
        try:
            cfg = json.loads(GORILLA_CONFIG.read_text())
        except (OSError, json.JSONDecodeError):
            return keys
        for e in cfg.get("localEndpoints", []) or []:
            if e.get("apiKey"):
                keys.append(e["apiKey"])
        for p in (cfg.get("providers") or {}).values():
            if isinstance(p, dict) and p.get("apiKey"):
                keys.append(p["apiKey"])
    for var in os.environ:
        if var.endswith(("_API_KEY", "_TOKEN")) and len(os.environ[var]) > 16:
            keys.append(os.environ[var])
    return [k for k in keys if len(k) > 16]


def scrub_text(text: str, keys: list[str]) -> tuple[str, int]:
    hits = 0
    for key in keys:
        if key in text:
            hits += text.count(key)
            text = text.replace(key, "<redacted-key>")
    for pattern, repl in PATTERNS:
        text, n = pattern.subn(repl, text)
        hits += n
    return text, hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="Report only; change nothing")
    ap.add_argument("--path", type=Path, default=HERE,
                    help="Directory to scan (default: this script's directory)")
    args = ap.parse_args()

    keys = live_keys()
    targets = sorted(
        [p for p in args.path.rglob("*.json") if "results" in p.parts]
        + [p for p in args.path.glob("*.log")]
    )
    if not targets:
        sys.exit(f"Nothing to scrub under {args.path}")

    changed = total = 0
    for path in targets:
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  skip {path.name}: {exc}")
            continue
        cleaned, hits = scrub_text(original, keys)
        if hits:
            changed += 1
            total += hits
            print(f"  {'would clean' if args.check else 'cleaned'} {path.name}: {hits} redaction(s)")
            if not args.check:
                path.write_text(cleaned, encoding="utf-8")

    print(f"\n{len(targets)} file(s) scanned, {changed} contained identifiers, "
          f"{total} redaction(s) {'pending' if args.check else 'applied'}.")
    if args.check and changed:
        sys.exit(1)  # non-zero so CI can gate a publish on this


if __name__ == "__main__":
    main()
