# pw-webhook

Watch PortWatch VI tracker and GitHub branch changes and deliver them to Discord.
Requires Python 3.9+ on Windows or Linux.

Install dependencies: `python -m pip install -r requirements.txt`.
Set `DISCORD_WEBHOOK_URL` in `.env` beside `portwatch.py`. Optional settings:
`SITE` (defaults to https://portwatch-vi.xyz, including when blank),
`WATCH_REPOS` (comma-separated owner/repo allowlist), and `GITHUB_TOKEN`.

Run `python portwatch.py` to check immediately and every three hours while running.
Use `python portwatch.py --once` for a single check and delivery, or
`python portwatch.py --dry-run` to check without sending or saving changes.
In the existing WSL environment, use `.venv/bin/python portwatch.py`.
Stop with Ctrl+C. The process must stay running for scheduled checks.

Every check sends a status embed, including the first baseline, unchanged checks,
and feed failures. Changes also produce individual notifications. Status embeds
include three inline counts for projects, GitHub branches, and skipped repositories.
Incomplete checks lead with "Tracker checked · GitHub checks incomplete" and use
amber. Completed unchanged checks are gray; updates are purple and lead with the
actual change. Identical GitHub errors are grouped; repository details stay in the
console. Clickable titles open PortWatch, and native Discord timestamps show local
time (the original check time is preserved through queue retries).

The main field highlights the active project closest to a full port, using
PortWatch's published `assets/js/scoring.js` model: milestone ladder to 50%, then
story, rendering, FPS, and stability to 100%, with manual score overrides honored.
Projects marked `nuked` are excluded. Private builds are included and labeled,
matching the site's overall headline; WATCH_REPOS limits this to watched projects.
This is a PortWatch progress score, not a release ETA or independently verified
completion. The scoring model was inspected on 2026-11-26.

Confirmed DMCA-block counts appear only when nonzero for the current check.
Only an explicit GitHub HTTP 451 response with `block.reason=dmca` counts as a
confirmed DMCA block. Other errors remain skipped checks; no claimant is inferred.
If Discord is unavailable, reports remain queued for retry.
Notifications use the name PortWatch VI. The
webhook's configured avatar is used for each notification. Failed deliveries
stay in the SQLite queue and retry on the next run/check. Keep the database beside
the script to preserve history. A crash after Discord accepts a message but before
the queue is updated may cause that message to be delivered again.
