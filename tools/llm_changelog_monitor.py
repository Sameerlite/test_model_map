#!/usr/bin/env python3
"""Monitor major LLM provider changelogs and optionally post Slack updates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(".llm-changelog-state.json")
DATE_PATTERN = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}$"
    r"|^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}$"
)
MONTH_HEADING_PATTERN = re.compile(
    r"^(?:January|February|March|April|May|June|July|August|September|October|November|December), \d{4}$"
)
BREAKING_PATTERN = re.compile(
    r"\b("
    r"breaking|deprecated?|deprecation|retired?|retirement|removed?|shutdown|shut down|"
    r"no longer|return an error|migrat(?:e|ing)|sunset|replac(?:e|ed|ing)"
    r")\b",
    re.IGNORECASE,
)
SIGNAL_PATTERN = re.compile(
    r"\b("
    r"api|model|released?|launched?|added?|new|feature|breaking|deprecated?|retired?|"
    r"removed?|updated?|available|ga|generally available|preview|beta|endpoint|"
    r"responses?|messages?|gemini|gpt|claude|opus|sonnet|haiku|embedding|veo|imagen|"
    r"function calling|tool|batch|realtime"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Provider:
    name: str
    display_url: str
    fetch_urls: tuple[str, ...]
    parser: str
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    extra_headers: bool = True


@dataclass
class Entry:
    provider: str
    date: str
    text: str
    url: str
    breaking: bool
    digest: str


PROVIDERS = (
    Provider(
        name="OpenAI",
        display_url="https://platform.openai.com/docs/changelog",
        fetch_urls=(
            "https://developers.openai.com/api/docs/changelog",
            "https://platform.openai.com/api/docs/changelog",
            "https://platform.openai.com/docs/changelog",
        ),
        parser="openai",
    ),
    Provider(
        name="Anthropic",
        display_url="https://docs.anthropic.com/en/release-notes/overview",
        fetch_urls=("https://docs.anthropic.com/en/release-notes/overview",),
        parser="dated",
    ),
    Provider(
        name="Google Gemini",
        display_url="https://ai.google.dev/gemini-api/docs/changelog",
        fetch_urls=(
            "https://ai.google.dev/gemini-api/docs/changelog?hl=en",
            "https://ai.google.dev/gemini-api/docs/changelog",
        ),
        parser="dated",
        user_agent="Mozilla/5.0",
        extra_headers=False,
    ),
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth += 1
        if tag in {
            "article",
            "br",
            "dd",
            "div",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "li",
            "p",
            "section",
            "td",
            "th",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {
            "article",
            "dd",
            "div",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "li",
            "p",
            "section",
            "td",
            "th",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def fetch_html(provider: Provider, timeout: int) -> str:
    headers = {
        "User-Agent": provider.user_agent,
        "Accept": "text/html",
    }
    if provider.extra_headers:
        headers.update(
            {
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": provider.display_url,
            }
        )
    errors: list[str] = []
    for url in provider.fetch_urls:
        for attempt in range(3):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{url}: {exc}")
                if attempt < 2:
                    time.sleep(2**attempt)
    raise RuntimeError(f"Unable to fetch {provider.name}: {'; '.join(errors)}")


def html_to_lines(html: str) -> list[str]:
    parser = TextExtractor()
    parser.feed(html)
    text = unescape("".join(parser.parts))
    return [line for line in (normalize_whitespace(part) for part in text.splitlines()) if line]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def summarize_text(lines: Iterable[str], max_chars: int = 900) -> str:
    text = normalize_whitespace(" ".join(lines))
    if len(text) <= max_chars:
        return text
    return textwrap.shorten(text, width=max_chars, placeholder="...")


def looks_like_date(line: str) -> bool:
    return bool(DATE_PATTERN.match(line.rstrip(".")))


def looks_relevant(text: str) -> bool:
    return bool(SIGNAL_PATTERN.search(text))


def parse_dated_entries(provider: Provider, lines: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if "Release notes" in line or "Changelog" in line
        ),
        0,
    )
    date_indexes = [
        index for index, line in enumerate(lines[start:], start=start) if looks_like_date(line)
    ]
    for position, index in enumerate(date_indexes):
        next_index = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        body_lines = lines[index + 1 : next_index]
        text = summarize_text(body_lines)
        if text and looks_relevant(text):
            entries.append(make_entry(provider, lines[index], text))
    return entries


def parse_openai_entries(provider: Provider, lines: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line == "Changelog" and index > 100
        ),
        0,
    )
    month = ""
    index = start
    while index < len(lines):
        line = lines[index]
        if MONTH_HEADING_PATTERN.match(line):
            month = line.replace(",", "")
            index += 1
            continue
        if month and looks_like_date(line):
            date = f"{line}, {month.split()[-1]}" if "," not in line else line
            next_index = index + 1
            while next_index < len(lines):
                if MONTH_HEADING_PATTERN.match(lines[next_index]) or looks_like_date(lines[next_index]):
                    break
                next_index += 1
            body_lines = [
                item
                for item in lines[index + 1 : next_index]
                if item not in {"Feature", "Update", "Fix", "Announcement"}
            ]
            text = summarize_text(body_lines)
            if text and looks_relevant(text):
                entries.append(make_entry(provider, date, text))
            index = next_index
            continue
        index += 1
    return entries


def make_entry(provider: Provider, date: str, text: str) -> Entry:
    normalized = normalize_whitespace(f"{provider.name}|{date}|{text}")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return Entry(
        provider=provider.name,
        date=date,
        text=text,
        url=provider.display_url,
        breaking=bool(BREAKING_PATTERN.search(text)),
        digest=digest,
    )


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "providers": {}}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, entries: list[Entry]) -> None:
    state = {
        "version": STATE_VERSION,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "providers": {},
    }
    for provider in PROVIDERS:
        provider_entries = [entry for entry in entries if entry.provider == provider.name]
        state["providers"][provider.name] = {
            "url": provider.display_url,
            "digests": [entry.digest for entry in provider_entries],
            "entries": [asdict(entry) for entry in provider_entries],
        }
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def previous_digests(state: dict) -> set[str]:
    digests: set[str] = set()
    for provider_state in state.get("providers", {}).values():
        digests.update(provider_state.get("digests", []))
    return digests


def collect_entries(timeout: int) -> list[Entry]:
    entries: list[Entry] = []
    for provider in PROVIDERS:
        html = fetch_html(provider, timeout=timeout)
        lines = html_to_lines(html)
        if provider.parser == "openai":
            provider_entries = parse_openai_entries(provider, lines)
        else:
            provider_entries = parse_dated_entries(provider, lines)
        if not provider_entries:
            raise RuntimeError(f"No changelog entries parsed for {provider.name}")
        entries.extend(provider_entries)
    return entries


def format_slack_message(new_entries: list[Entry]) -> str:
    grouped: dict[str, list[Entry]] = {}
    for entry in new_entries:
        grouped.setdefault(entry.provider, []).append(entry)

    lines = ["*LLM provider changelog updates*"]
    breaking = [entry for entry in new_entries if entry.breaking]
    if breaking:
        lines.append("")
        lines.append("*Breaking changes / deprecations*")
        for entry in breaking:
            lines.append(f"- *{entry.provider}* ({entry.date}): {entry.text} <{entry.url}|source>")

    lines.append("")
    lines.append("*All changes*")
    for provider, entries in grouped.items():
        lines.append(f"*{provider}*")
        for entry in entries:
            prefix = "[BREAKING] " if entry.breaking else ""
            lines.append(f"- {prefix}{entry.date}: {entry.text} <{entry.url}|source>")
    return "\n".join(lines)


def post_to_slack(message: str, webhook_url: str, channel: str | None) -> None:
    payload: dict[str, str] = {"text": message}
    if channel:
        payload["channel"] = channel
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Slack webhook failed with HTTP {response.status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Print the Slack message only.")
    parser.add_argument(
        "--alert-on-first-run",
        action="store_true",
        help="Treat all currently parsed entries as new when no state file exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    old_state = load_state(args.state)
    old_digests = previous_digests(old_state)
    entries = collect_entries(timeout=args.timeout)
    first_run = not args.state.exists()
    new_entries = [
        entry
        for entry in entries
        if entry.digest not in old_digests and (old_digests or args.alert_on_first_run)
    ]

    save_state(args.state, entries)

    if not new_entries:
        print("No new changelog entries found.")
        return 0

    message = format_slack_message(new_entries)
    if args.dry_run:
        print(message)
    else:
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook_url:
            if first_run:
                print("State initialized. Set SLACK_WEBHOOK_URL to post future updates.")
                return 0
            print("SLACK_WEBHOOK_URL is required when new entries are found.", file=sys.stderr)
            return 2
        post_to_slack(message, webhook_url, os.environ.get("SLACK_CHANNEL", "#llm-updates"))
        print(f"Posted {len(new_entries)} new changelog entries to Slack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
