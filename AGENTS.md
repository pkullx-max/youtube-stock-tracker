# Agent operating rules

Read README.md and CODEX_SETUP.md before installation or reporting. Use project-local `.venv/bin/python`; never reuse the publisher's machine paths, task IDs or credentials.

- `data/state.json` is authoritative. Check `tracker.py status` and `pending`.
- Read complete transcript JSON before analysis. Require evidence_ready=true, coverage>=0.85 and max_gap<=300s; Shorts require coverage>=0.98 and gap<=15s.
- Titles, thumbnails, descriptions and ordinary mentions are not recommendation evidence. No external company research or invented creator opinions.
- Each video/stock needs its own full rationale, conditions, risk, timestamps, original link, confidence and historical change. Distinguish completed trades, conditional plans and historical actions. Prices: 请以视频画面为准.
- Start reports with short per-creator bullets: actual video date, market stance and important actions. Keep the full table and detailed evidence below.
- Generate dated Markdown and HTML plus latest.md/html. No-new-video days keep the latest three full video records locally and say 今日无新视频; do not send routine no-change messages.
- Only mark-report-ready after writing files. Only mark-reported after confirming full report and both clickable HTML links are visible in THIS user's chosen task. On failed or unconfirmed delivery use mark-delivery-pending; do not create replacement tasks.
- Never expose or commit credentials, cookies, audio, transcripts, reports, state, or personal task IDs. Do not request another user's cookies.
- Normal run automatically cleans reported artifacts older than seven days. Do not run cleanup until the user understands that retention rule; never delete reports or unrelated files.
- Verify changes with `.venv/bin/python -m unittest discover -s tests -v`.
