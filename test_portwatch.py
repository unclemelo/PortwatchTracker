import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import portwatch as watcher


class CheckReportsTests(unittest.TestCase):
    def test_baseline_and_unchanged_checks_both_report(self):
        db = watcher.open_database(":memory:")
        self.addCleanup(db.close)
        feed = {"efforts": [{"id": "demo", "name": "Demo"}]}
        for expected in ("Baseline recorded", "No progress changes"):
            changes, messages = watcher.collect(db, feed, set(), None)
            self.assertEqual(len(messages), 1)
            embed = watcher.notification_payload(messages[0])["embeds"][0]
            self.assertIn(expected, embed["description"])
            self.assertEqual(next(f["value"] for f in embed["fields"] if f["name"] == "Repos skipped"), "0")
            self.assertNotIn("Confirmed DMCA blocks", [f["name"] for f in embed["fields"]])
            watcher.save_updates(db, changes, messages)

    def test_only_explicit_dmca_blocks_count_and_duplicates_are_skipped(self):
        db = watcher.open_database(":memory:")
        self.addCleanup(db.close)
        rows = [{"id": str(i), "name": repo, "repo": repo} for i, repo in enumerate(
            ["owner/missing", "owner/dmca", "owner/legal", "owner/missing"])]
        errors = [HTTPError("https://example.com", code, "blocked", {}, io.BytesIO(json.dumps(body).encode()))
                  for code, body in [(404, {}), (451, {"block": {"reason": "dmca"}}), (451, {})]]
        with patch.object(watcher, "fetch", side_effect=errors) as fetch:
            _, messages = watcher.collect(db, {"efforts": rows}, set(), None)
        self.assertEqual(fetch.call_count, 3)
        embed = messages[0]["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(fields["Repos skipped"], "3")
        self.assertIn("1 repository", fields["Confirmed DMCA blocks"])
        self.assertIn("GitHub checks incomplete", embed["description"])
        self.assertEqual(embed["color"], watcher.AMBER)

    def test_feed_failure_queues_and_delivers_failure_embed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(watcher, "ROOT", Path(directory)), \
                 patch.object(watcher, "load_dotenv"), \
                 patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.com/webhook"}), \
                 patch("sys.argv", ["portwatch.py", "--once"]), \
                 patch.object(watcher, "fetch", side_effect=[ValueError("invalid feed"), {"id": "sent"}]) as fetch:
                self.assertEqual(watcher.main(), 1)
                embed = fetch.call_args.kwargs["payload"]["embeds"][0]
                self.assertIn("Check failed", embed["description"])
                self.assertIn("counts unavailable", embed["description"])

    def test_all_403_grouped_and_timestamp_survives_queue(self):
        rows = [{"name": "Demo", "gta": {}}] * 44
        skipped = {f"owner/repo{i}": "HTTP 403" for i in range(38)}
        message = watcher.progress_report(rows, 0, skipped, set(), False, [])
        embed = message["embeds"][0]
        self.assertTrue(embed["description"].startswith("🟡 **Tracker checked · GitHub checks incomplete**"))
        self.assertIn("No progress changes across 44", embed["description"])
        fields = {f["name"]: f for f in embed["fields"]}
        self.assertIn("38 repositories returned `HTTP 403`", fields["GitHub status"]["value"])
        self.assertEqual([fields[k]["value"] for k in ("Projects checked", "GitHub branches checked", "Repos skipped")], ["44", "0", "38"])
        self.assertTrue(all(fields[k]["inline"] for k in ("Projects checked", "GitHub branches checked", "Repos skipped")))
        db = watcher.open_database(":memory:")
        self.addCleanup(db.close)
        watcher.save_updates(db, {}, [message])
        stored = db.execute("SELECT body FROM queue").fetchone()[0]
        self.assertEqual(watcher.notification_payload(stored), message)
        self.assertTrue(embed["timestamp"].endswith("+00:00"))

    def test_scoring_and_leader_excludes_nuked(self):
        gta = dict.fromkeys(["loads", "boots", "menu", "world", "playable"], 1)
        gta.update(story=.8, render=.8, fps=22, stability=1)
        self.assertEqual(watcher.port_score({"gta": gta}), 91.3)
        self.assertEqual(watcher.port_score({"gta": {"loads": 1, "boots": .5, "fps": 30}}), 15)
        self.assertEqual(watcher.port_score({"manualScore": 101}), 100)
        field = watcher.leader_field([{"name": "Offline", "manualScore": 100, "nuked": True},
                                      {"name": "Leader", "gta": gta}])
        self.assertIn("Leader · 91.3%", field["value"])
        self.assertIn("private build", field["value"])

    def test_fps_change_leads_with_actual_change(self):
        db = watcher.open_database(":memory:")
        self.addCleanup(db.close)
        row = {"id": "demo", "name": "Demo", "gta": {"fps": 5}}
        changes, messages = watcher.collect(db, {"efforts": [row]}, set(), None)
        watcher.save_updates(db, changes, messages)
        row["gta"]["fps"] = 10
        _, messages = watcher.collect(db, {"efforts": [row]}, set(), None)
        self.assertIn("FPS increased from 5 to 10", messages[0]["embeds"][0]["description"])
        self.assertIn("FPS increased from 5 to 10", messages[1]["embeds"][0]["title"])
        self.assertEqual(messages[0]["embeds"][0]["color"], watcher.PURPLE)
        self.assertIn("embeds", watcher.notification_payload("**Legacy title**\nLegacy body"))


if __name__ == "__main__":
    unittest.main()
