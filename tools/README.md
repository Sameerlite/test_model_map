# LLM Changelog Monitor

`llm_changelog_monitor.py` checks the OpenAI, Anthropic, and Google Gemini API
changelogs for newly seen entries. It stores entry hashes in a JSON state file,
then posts a Slack summary only when a later run discovers new entries.

The existing Cursor automation schedule should run this command daily at 9am:

```sh
python3 tools/llm_changelog_monitor.py
```

Configuration:

- `SLACK_WEBHOOK_URL`: incoming webhook used to post updates.
- `SLACK_CHANNEL`: optional Slack channel override. Defaults to `#llm-updates`.
- `--state`: path to the comparison state file. Defaults to
  `.llm-changelog-state.json`.
- `--dry-run`: print the message instead of posting to Slack.
- `--alert-on-first-run`: report all currently parsed entries when initializing
  a new state file. By default, first run only initializes state.

The Slack message groups changes by provider and repeats deprecations, removals,
retirements, migrations, and other breaking-change language in a separate
section.
