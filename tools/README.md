# LLM Changelog Monitor

This folder contains a small dependency-free helper for the daily LLM provider
changelog automation.

The automation checks:

- OpenAI: https://platform.openai.com/docs/changelog
- Anthropic: https://docs.anthropic.com/en/release-notes/overview
- Google Gemini API: https://ai.google.dev/gemini-api/docs/changelog

The persistent automation memory remains the source of truth for production
state. The script is a reproducible local helper for comparing rendered page
content against a JSON baseline.

## Usage

Save rendered page text or Markdown as:

- `openai.md`
- `anthropic.md`
- `google.md`

Then run:

```bash
python3 tools/llm_changelog_monitor.py \
  --state /path/to/llm_changelog_watch_state.json \
  --content-dir /path/to/rendered-pages
```

Add `--write-state` to update the JSON baseline after reviewing or posting the
summary.
