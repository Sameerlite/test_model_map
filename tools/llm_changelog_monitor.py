#!/usr/bin/env python3
"""Compare rendered LLM provider changelog pages against a JSON baseline.

This helper is intentionally dependency-free so it can be run inside a Cursor
automation or locally with copied WebFetch output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import textwrap
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "url": "https://platform.openai.com/docs/changelog",
        "file": "openai.md",
    },
    "anthropic": {
        "name": "Anthropic",
        "url": "https://docs.anthropic.com/en/release-notes/overview",
        "file": "anthropic.md",
    },
    "google": {
        "name": "Google Gemini API",
        "url": "https://ai.google.dev/gemini-api/docs/changelog",
        "file": "google.md",
    },
}

TRACKING_KEYWORDS = (
    "api",
    "model",
    "released",
    "launched",
    "feature",
    "breaking",
    "deprecat",
    "retired",
    "shut down",
    "schema",
    "endpoint",
    "beta",
    "generally available",
    "ga",
)

BREAKING_KEYWORDS = (
    "breaking",
    "deprecat",
    "retired",
    "shut down",
    "removed",
    "return an error",
    "legacy",
    "migrat",
)


@dataclass(frozen=True)
class Entry:
    provider: str
    date: str
    category: str
    summary: str
    breaking: bool

    @property
    def key(self) -> str:
        return f"{self.date}|{self.summary}"


def clean_text(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -\t\n\r")


def strip_html(value: str) -> str:
    value = re.sub(r"<(h[1-6]|p|li|br|div)[^>]*>", "\n", value, flags=re.I)
    value = re.sub(r"</(h[1-6]|p|li|div)>", "\n", value, flags=re.I)
    return clean_text(value)


def month_to_iso(month: str, day: str, year: str = "2026") -> str:
    month_num = dt.datetime.strptime(month[:3], "%b").month
    return f"{int(year):04d}-{month_num:02d}-{int(day):02d}"


def classify(summary: str, explicit_kind: str = "") -> tuple[str, bool]:
    lowered = f"{explicit_kind} {summary}".lower()
    breaking = any(keyword in lowered for keyword in BREAKING_KEYWORDS)
    if breaking and any(word in lowered for word in ("released", "launched", "feature", "supports")):
        category = "feature/breaking"
    elif breaking:
        category = "breaking"
    elif any(word in lowered for word in ("released", "launched", "supports", "added", "available")):
        category = "feature"
    else:
        category = explicit_kind.lower() or "update"
    return category, breaking


def is_tracked(summary: str, explicit_kind: str = "") -> bool:
    lowered = f"{explicit_kind} {summary}".lower()
    return any(keyword in lowered for keyword in TRACKING_KEYWORDS)


def summarize_sentences(text: str, max_chars: int = 360) -> str:
    text = clean_text(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = ""
    for sentence in sentences:
        candidate = f"{summary} {sentence}".strip()
        if len(candidate) > max_chars and summary:
            break
        summary = candidate
    if not summary:
        summary = text[:max_chars]
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "."
    return summary


def parse_anthropic(text: str) -> list[Entry]:
    entries: list[Entry] = []
    sections = re.split(r"(?m)^###\s+", text)
    for section in sections:
        heading = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s*\n(.*)", section, re.S)
        if not heading:
            continue
        month, day, year, body = heading.groups()
        date = month_to_iso(month, day, year)
        bullets = re.findall(r"(?m)^-\s+(.*?)(?=\n-\s+|\Z)", body.strip(), re.S)
        for bullet in bullets:
            summary = summarize_sentences(bullet)
            if not is_tracked(summary):
                continue
            category, breaking = classify(summary)
            entries.append(Entry("anthropic", date, category, summary, breaking))
    return entries


def parse_google(text: str) -> list[Entry]:
    entries: list[Entry] = []
    sections = re.split(r"(?m)^##\s+", text)
    for section in sections:
        heading = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s*\n(.*)", section, re.S)
        if not heading:
            continue
        month, day, year, body = heading.groups()
        date = month_to_iso(month, day, year)
        bullets = re.findall(r"(?m)^-\s+(.*?)(?=\n-\s+|\Z)", body.strip(), re.S)
        for bullet in bullets:
            summary = summarize_sentences(bullet)
            if not is_tracked(summary):
                continue
            category, breaking = classify(summary)
            entries.append(Entry("google", date, category, summary, breaking))
    return entries


def parse_openai(text: str) -> list[Entry]:
    entries: list[Entry] = []
    active_year = "2026"
    lines = [line.strip() for line in text.splitlines()]
    i = 0
    while i < len(lines):
        year_match = re.match(r"^###\s+([A-Za-z]+),\s+(\d{4})$", lines[i])
        if year_match:
            active_year = year_match.group(2)
            i += 1
            continue

        date_match = re.match(r"^([A-Z][a-z]{2})\s+(\d{1,2})$", lines[i])
        if not date_match:
            i += 1
            continue

        date = month_to_iso(date_match.group(1), date_match.group(2), active_year)
        explicit_kind = lines[i + 1].strip() if i + 1 < len(lines) else ""
        j = i + 2
        chunks: list[str] = []
        while j < len(lines):
            if re.match(r"^([A-Z][a-z]{2})\s+\d{1,2}$", lines[j]) or lines[j].startswith("### "):
                break
            if lines[j]:
                chunks.append(lines[j])
            j += 1

        body = " ".join(chunks)
        # Drop endpoint/model labels until the first prose sentence.
        prose_match = re.search(r"(Added|Announced|Expanded|Fixed|Launched|Released|Updated|We\s+|The\s+).*$", body, re.I)
        summary = summarize_sentences(prose_match.group(0) if prose_match else body)
        if is_tracked(summary, explicit_kind):
            category, breaking = classify(summary, explicit_kind)
            entries.append(Entry("openai", date, category, summary, breaking))
        i = j
    return entries


def parse_entries(provider: str, text: str) -> list[Entry]:
    if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
        text = strip_html(text)
    if provider == "openai":
        return parse_openai(text)
    if provider == "anthropic":
        return parse_anthropic(text)
    if provider == "google":
        return parse_google(text)
    raise ValueError(f"unknown provider: {provider}")


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "llm-changelog-monitor/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def read_provider_text(provider: str, content_dir: Path | None) -> str:
    meta = PROVIDERS[provider]
    if content_dir is not None:
        return (content_dir / meta["file"]).read_text(encoding="utf-8")
    return fetch_url(meta["url"])


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"providers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def diff_entries(entries: Iterable[Entry], known_keys: set[str]) -> list[Entry]:
    return [entry for entry in entries if entry.key not in known_keys]


def render_summary(changes: list[Entry]) -> str:
    if not changes:
        return "No new tracked API features, model releases, or breaking changes."

    lines = ["LLM provider changelog updates detected:"]
    for provider in PROVIDERS:
        provider_changes = [entry for entry in changes if entry.provider == provider]
        if not provider_changes:
            continue
        lines.append("")
        lines.append(f"{PROVIDERS[provider]['name']}:")
        for entry in provider_changes:
            lines.append(f"- {entry.date} | {entry.category} | {entry.summary}")

    breaking = [entry for entry in changes if entry.breaking]
    lines.append("")
    lines.append("Breaking changes:")
    if breaking:
        for entry in breaking:
            lines.append(f"- {PROVIDERS[entry.provider]['name']} {entry.date}: {entry.summary}")
    else:
        lines.append("- None newly detected.")
    return "\n".join(lines)


def update_state(state: dict, parsed: dict[str, list[Entry]], changes: list[Entry]) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state["last_checked"] = now
    state["last_result"] = "changes_detected" if changes else "no_changes"
    state.setdefault("providers", {})
    for provider, entries in parsed.items():
        state["providers"][provider] = [entry.key for entry in entries[:10]]
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare LLM provider changelogs against a JSON baseline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Content directory mode expects:
              openai.md
              anthropic.md
              google.md
            """
        ),
    )
    parser.add_argument("--state", type=Path, required=True, help="Path to baseline JSON state.")
    parser.add_argument("--content-dir", type=Path, help="Directory containing rendered provider markdown/text.")
    parser.add_argument("--write-state", action="store_true", help="Persist the latest parsed keys to --state.")
    args = parser.parse_args(argv)

    state = load_state(args.state)
    parsed: dict[str, list[Entry]] = {}
    changes: list[Entry] = []

    for provider in PROVIDERS:
        text = read_provider_text(provider, args.content_dir)
        entries = parse_entries(provider, text)
        parsed[provider] = entries
        known = set(state.get("providers", {}).get(provider, []))
        changes.extend(diff_entries(entries[:10], known))

    print(render_summary(changes))

    if args.write_state:
        state = update_state(state, parsed, changes)
        args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
