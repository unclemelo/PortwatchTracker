"""PortWatch VI -> Discord. Run on Windows or Linux with Python 3.9+.
Install with python -m pip install python-dotenv "colorama>=0.4.6". Set DISCORD_WEBHOOK_URL in .env beside this script. Optional WATCH_REPOS is a
comma-separated owner/repo allowlist. Optional GITHUB_TOKEN raises API limits.
Keep portwatch.sqlite3 beside this file. Stop with Ctrl+C.
"""
import argparse
from collections import Counter
import math
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen

from colorama import Fore, Style, just_fix_windows_console
from dotenv import load_dotenv

SITE = "https://portwatch-vi.xyz"
INTERVAL = 1 * 3600
ROOT = Path(__file__).resolve().parent
REPO = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)


LOG_COLORS = {
    "WATCH": Fore.MAGENTA,
    "INFO": Fore.CYAN,
    "OK": Fore.GREEN,
    "WARN": Fore.YELLOW,
    "ERROR": Fore.RED,
}


def log(level, message):
    label = f"[{level}]"
    if sys.stdout.isatty() and not os.getenv("NO_COLOR"):
        label = f"{Style.BRIGHT}{LOG_COLORS[level]}{label}{Style.RESET_ALL}"
    print(f"{datetime.now():%H:%M:%S}  {label}  {message}", flush=True)


def fetch(url, payload=None, token=None):
    headers = {"User-Agent": "PortWatch-Discord-Watcher/1.0"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, data=data, headers=headers), timeout=20) as res:
        body = res.read()
        return json.loads(body) if body else None


def progress(row):
    return {"name": row["name"], "repo": row.get("repo"),
            "tier": row.get("tier"), **(row.get("gta") or {})}


def github_skip_reason(exc):
    """Only classify a DMCA block when GitHub explicitly identifies it."""
    if isinstance(exc, HTTPError):
        if exc.code == 451:
            try:
                data = json.loads(exc.read())
                block = data.get("block") if isinstance(data, dict) else None
                if isinstance(block, dict) and block.get("reason") == "dmca":
                    return "DMCA block confirmed by GitHub", True
            except (ValueError, OSError):
                pass
        return f"HTTP {exc.code}", False
    return type(exc).__name__, False


PURPLE = 0xBD40CE
AMBER = 0xF0B232
GRAY = 0x747F8D


def embed_message(title, description, color=PURPLE, fields=None):
    embed = {"title": title[:256], "url": SITE, "description": description[:4096],
             "color": color, "timestamp": datetime.now(timezone.utc).isoformat(),
             "footer": {"text": "PortWatch VI"}}
    if fields:
        embed["fields"] = fields
    return {"username": "PortWatch VI", "embeds": [embed], "allowed_mentions": {"parse": []}}


def clamp(value):
    try:
        number = float(value)
        return min(1, max(0, number)) if math.isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def port_score(row):
    """PortWatch's assets/js/scoring.js model, inspected 2026-11-26."""
    gta = row.get("gta") or {}
    groundwork = row.get("groundwork") or {}
    score = 10 * sum(weight * clamp(groundwork.get(key)) for key, weight in
                     [("loader", .2), ("shaders", .28), ("graphics", .32), ("proven", .2)])
    for tick, key in enumerate(["loads", "boots", "menu", "world", "playable"], 1):
        value = clamp(gta.get(key))
        if value < 1:
            score += value * (tick * 10 - score)
            break
        score = tick * 10
    else:
        stability = gta.get("stability")
        if not isinstance(stability, (int, float)):
            stability = {"constant": 0, "often": .25, "sometimes": .55,
                         "rarely": .8, "stable": 1}.get(stability, 0)
        try:
            performance = clamp(float(gta.get("fps") or 0) / 30)
        except (TypeError, ValueError):
            performance = 0
        score = 50 + 50 * (.35 * clamp(gta.get("story")) + .25 * clamp(gta.get("render"))
                           + .2 * performance + .2 * clamp(stability))
    manual = row.get("manualScore")
    if type(manual) in (int, float) and math.isfinite(manual):
        score = min(100, max(0, manual))
    return math.floor(score * 10 + .5) / 10


def leader_field(rows):
    eligible = [row for row in rows if not row.get("nuked")]
    if not eligible:
        return {"name": "Closest to a full port", "value": "No active projects available to rank.", "inline": False}
    leader = max(eligible, key=lambda row: (port_score(row), (row.get("stats") or {}).get("stars", 0)))
    visibility = "public repository" if REPO.fullmatch(leader.get("repo") or "") else "private build"
    return {"name": "Closest to a full port",
            "value": f"**{str(leader['name'])[:120]} · {port_score(leader):g}%** ({visibility})\n"
                     f"[PortWatch progress score]({SITE}/#method), not a release ETA or independently verified completion.",
            "inline": False}


def check_report(status, details):
    return embed_message("PortWatch VI · Progress report", f"🟡 **{status}**\n{details}", AMBER)


def progress_report(rows, checked_branches, skipped, dmca_repos, baseline, updates):
    if skipped:
        headline = "🟡 **Tracker checked · GitHub checks incomplete**"
        color = AMBER
    elif updates:
        headline = f"🟣 **{updates[0]['headline']}**"
        color = PURPLE
    else:
        headline = "**Tracker checked · GitHub checks complete**"
        color = GRAY
    if baseline:
        summary = f"Baseline recorded across {len(rows)} tracked projects. Future checks will compare changes."
    else:
        tracker_updates = sum(1 for item in updates if item.get("kind") == "tracker")
        summary = (f"{tracker_updates} project updates across {len(rows)} tracked projects." if tracker_updates
                   else f"No progress changes across {len(rows)} tracked projects.")
    if updates and skipped:
        headline += "\n" + updates[0]["headline"]
    fields = [leader_field(rows)]
    fields.extend({"name": name, "value": str(value), "inline": True} for name, value in
                  [("Projects checked", len(rows)), ("GitHub branches checked", checked_branches),
                   ("Repos skipped", len(skipped))])
    if skipped:
        counts = Counter(skipped.values())
        grouped = "\n".join(f"{count} {'repository' if count == 1 else 'repositories'} returned `{reason}`."
                            for reason, count in sorted(counts.items()))
        grouped += "\nCommit activity is unknown for " + ("this check." if not checked_branches else "the skipped repositories.")
        fields.append({"name": "GitHub status", "value": grouped[:1024], "inline": False})
    if dmca_repos:
        count = len(dmca_repos)
        noun = "repository" if count == 1 else "repositories"
        fields.append({"name": "Confirmed DMCA blocks", "value": f"{count} {noun} blocked by GitHub. Claimant not verified.", "inline": False})
    return embed_message("PortWatch VI · Progress report", headline + "\n" + summary, color, fields)


def collect(db, feed, allow, token):
    rows = feed["efforts"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Empty or invalid efforts feed")
    saved = dict(db.execute("SELECT key, value FROM snapshot"))
    changes = {}
    messages = []
    checked_branches = 0
    skipped = {}
    dmca_repos = set()
    attempted = set()
    watched_rows = []
    updates = []
    for row in rows:
        repo = row.get("repo") or ""
        if allow and repo.lower() not in allow:
            continue
        watched_rows.append(row)
        key = "project/" + row["id"]
        current = progress(row)
        changes[key] = current
        name = str(row["name"])[:120]
        if key in saved:
            old = json.loads(saved[key])
            lines = [f"{field}: {old.get(field, 'unknown')} → {current.get(field, 'unknown')}"
                     for field in sorted(current.keys() | old.keys())
                     if old.get(field) != current.get(field)]
            if lines:
                fields_changed = [field for field in current if old.get(field) != current.get(field)]
                field = next((key for key in ["fps", "story", "render", "stability", "playable"] if key in fields_changed), fields_changed[0] if fields_changed else "progress")
                label = "FPS" if field == "fps" else field.replace("_", " ").capitalize()
                before, after = old.get(field, "unknown"), current.get(field, "unknown")
                direction = "changed"
                if type(before) in (int, float) and type(after) in (int, float):
                    direction = "increased" if after > before else "decreased"
                headline = f"{name} · {label} {direction} from {before} to {after}"
                updates.append({"kind": "tracker", "headline": headline[:256]})
                messages.append(embed_message(headline, "\n".join(lines)))
        elif saved:
            headline = f"New to this watch · {name}"
            updates.append({"kind": "tracker", "headline": headline})
            messages.append(embed_message(headline, "Project added to the tracker."))
        if not REPO.fullmatch(repo):
            continue
        branch = row.get("branch") or "HEAD"
        key = f"git/{repo.lower()}/{branch}"
        if key in attempted:
            continue
        attempted.add(key)
        try:
            head = fetch(f"https://api.github.com/repos/{repo}/commits/{quote(branch, safe='')}", token=token)
            sha = head["sha"]
            if not re.fullmatch("[0-9a-f]{40}", sha):
                raise ValueError("Invalid commit SHA")
            changes[key] = sha
            checked_branches += 1
            if key in saved and json.loads(saved[key]) != sha:
                old_sha = json.loads(saved[key])
                title = head["commit"]["message"].splitlines() or ["Commit"]
                headline = f"{repo} · branch updated"
                updates.append({"kind": "git", "headline": headline})
                message = embed_message(headline, title[0][:200])
                message["embeds"][0]["url"] = f"https://github.com/{repo}/compare/{old_sha}...{sha}"
                messages.append(message)
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
            detail, dmca = github_skip_reason(exc)
            skipped[repo.lower()] = detail
            if dmca:
                dmca_repos.add(repo.lower())
            log("WARN", f"Skipped GitHub check for {repo}: {detail}")
    if allow and not changes:
        raise ValueError("No watched repositories matched the tracker")
    project_count = sum(key.startswith("project/") for key in changes)
    if not saved:
        status = "Baseline recorded."
    elif messages:
        status = f"{len(messages)} updates found."
    else:
        status = "No changes found."
    log("WARN" if skipped else "OK", f"{status} {project_count} projects; {checked_branches} branches checked; "
          f"{len(skipped)} skipped.")
    messages.insert(0, progress_report(watched_rows, checked_branches, skipped, dmca_repos, not saved, updates))
    return changes, messages


def open_database(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS snapshot(key TEXT PRIMARY KEY,value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY,body TEXT)")
    db.commit()
    return db


def save_updates(db, changes, messages):
    # Persist notifications and their snapshot together so a failed send can retry.
    with db:
        db.executemany("INSERT OR REPLACE INTO snapshot(key,value) VALUES (?,?)",
                       [(key, json.dumps(value)) for key, value in changes.items()])
        for message in messages:
            if isinstance(message, dict):
                db.execute("INSERT INTO queue(body) VALUES (?)", (json.dumps(message),))
                continue
            for start in range(0, len(message), 1900):
                db.execute("INSERT INTO queue(body) VALUES (?)", (message[start:start + 1900],))


def notification_payload(body):
    """Render both existing queued messages and new notifications as embeds."""
    if isinstance(body, dict):
        return body
    try:
        stored = json.loads(body)
        if isinstance(stored, dict) and "embeds" in stored:
            return stored
    except ValueError:
        pass
    lines = body.splitlines()
    title = "PortWatch VI update"
    if lines and lines[0].startswith("**") and lines[0].endswith("**"):
        title = lines.pop(0)[2:-2]
    url = SITE
    if lines and re.fullmatch(r"<https?://[^<>\s]+>", lines[-1]):
        url = lines.pop()[1:-1]
    embed = {
        "title": title[:256],
        "url": url,
        "color": 0xBD40CE,
        "footer": {"text": "PortWatch VI · Tracker & repository updates"},
    }
    description = "\n".join(lines).strip()
    if description:
        embed["description"] = description[:4096]
    return {"username": "PortWatch VI", "embeds": [embed],
            "allowed_mentions": {"parse": []}}


def deliver(db, webhook):
    parts = urlsplit(webhook)
    query = dict(parse_qsl(parts.query))
    query["wait"] = "true"
    url = urlunsplit(parts._replace(query=urlencode(query)))
    for message_id, body in db.execute("SELECT id,body FROM queue ORDER BY id").fetchall():
        fetch(url, payload=notification_payload(body))
        with db:
            db.execute("DELETE FROM queue WHERE id=?", (message_id,))
        log("OK", f"Delivered queued message {message_id}.")


@contextmanager
def single_instance(path):
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another PortWatch instance is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another PortWatch instance is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    global SITE
    just_fix_windows_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check and deliver once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Check once without sending or saving")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    SITE = (os.getenv("SITE") or SITE).rstrip("/")
    webhook = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook and not args.dry_run:
        parser.error("Set DISCORD_WEBHOOK_URL in .env")
    allow = {repo.strip().lower() for repo in os.getenv("WATCH_REPOS", "").split(",") if repo.strip()}
    token = os.getenv("GITHUB_TOKEN")
    with single_instance(ROOT / "portwatch.lock"):
        mode = "dry run" if args.dry_run else "single check" if args.once else "watching every 1 hour"
        log("WATCH", f"PortWatch VI · {mode}")
        if args.dry_run:
            db = open_database(":memory:")
            if (ROOT / "portwatch.sqlite3").exists():
                source = sqlite3.connect((ROOT / "portwatch.sqlite3").as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(db)
                finally:
                    source.close()
        else:
            db = open_database(ROOT / "portwatch.sqlite3")
        try:
            while True:
                try:
                    check_failed = False
                    try:
                        changes, messages = collect(db, fetch(SITE + "/data/tracker.json"), allow, token)
                    except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
                        detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
                        changes = {}
                        messages = [check_report("Check failed: " + detail,
                                    "Repository and DMCA counts unavailable for this check.")]
                        check_failed = True
                        log("ERROR", f"Check failed: {detail}.")
                    if args.dry_run:
                        log("INFO", f"Dry run: {len(messages)} notifications; nothing sent or saved.")
                    else:
                        save_updates(db, changes, messages)
                        deliver(db, webhook)
                    if check_failed and (args.once or args.dry_run):
                        return 1
                except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
                    # Do not log exception URLs: Discord URLs contain the webhook token.
                    detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
                    log("ERROR", f"Check failed: {detail}. Pending messages retained.")
                    if args.once or args.dry_run:
                        return 1
                if args.once or args.dry_run:
                    return 0
                log("INFO", "Next check in 1 hour.")
                time.sleep(INTERVAL)
        finally:
            db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("INFO", "Stopped.")
    except RuntimeError as exc:
        log("ERROR", str(exc))
        raise SystemExit(1)
