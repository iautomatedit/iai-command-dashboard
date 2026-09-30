# Notes for Claude

- `paper-trading/` is a research bot that observes markets and logs hypothetical
  trades. It never trades. Keep it that way: no exchange keys, no order code.
- Use the TypeSafe skill (`typesafe@typesafe-ai`, enabled in `.claude/settings.json`)
  when working on anything involving Jev or TypeSafe. Read the live docs at
  https://docs.typesafe.ai before changing the integration in `paper-trading/ptbot/jev.py`.
- Every strategy or model must beat its baseline on out-of-sample data before it
  gets a trading role, even a paper one. Report dead results plainly.
- Copy rules: no em dashes, no guaranteed-outcome language.
