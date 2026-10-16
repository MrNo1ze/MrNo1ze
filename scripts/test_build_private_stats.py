"""Проверки сборщика без сети, секретов и записи в рабочие assets."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import build_private_stats as stats

NOW = datetime.now(timezone.utc)
REPOS = [
    {"full_name": "MrNo1ze/secret-a", "private": True, "archived": False,
     "fork": False, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
    {"full_name": "MrNo1ze/secret-b", "private": True, "archived": True,
     "fork": True, "pushed_at": (NOW - timedelta(days=31)).isoformat()},
]
FIXTURE: stats.Stats = {
    "generated_at": "2026-10-16T22:30:00+00:00", "total": 2, "archived": 1,
    "forks": 1, "pushed_30d": 1, "without_languages": 0, "coverage_verified": True,
    "languages": {"Go": {"bytes": 9_999_999, "repositories": 2},
                  **{f"Language {i}": {"bytes": i + 1, "repositories": 1} for i in range(13)},
                  "A<&\" very long language name for layout": {"bytes": 1, "repositories": 1}},
}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GITHUB_REPOSITORY_OWNER": "MrNo1ze"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_pagination_counts_bytes_and_repository_presence(self):
        pages = [
            ({"login": "MrNo1ze", "owned_private_repos": 2}, {}),
            ([REPOS[0]], {"link": '<https://api.github.com/page2>; rel="next"'}),
            ([REPOS[1]], {}),
            ({"Go": 100, "Shell": 5}, {}), ({"Go": 200, "Zero": 0}, {}),
        ]
        with patch.object(stats, "api", side_effect=pages) as api:
            result = stats.collect("test-token")
        self.assertEqual(result["languages"], {"Go": {"bytes": 300, "repositories": 2},
                                               "Shell": {"bytes": 5, "repositories": 1}})
        self.assertEqual((result["total"], result["archived"], result["forks"], result["pushed_30d"]), (2, 1, 1, 1))
        self.assertTrue(result["coverage_verified"])
        self.assertEqual(api.call_args_list[2].args[0], "https://api.github.com/page2")
        self.assertNotIn("secret-", json.dumps(result))

    def test_empty_and_partial_selection_are_errors(self):
        for repos, expected in (([], 0), ([REPOS[0]], 2)):
            with self.subTest(expected=expected), patch.object(stats, "api", side_effect=[
                ({"login": "MrNo1ze", "owned_private_repos": expected}, {}), (repos, {}),
            ]), self.assertRaisesRegex(ValueError, "Неполная"):
                stats.collect("test-token")

    def test_foreign_token_owner_is_rejected(self):
        with patch.object(stats, "api", return_value=({"login": "someone-else"}, {})), self.assertRaisesRegex(ValueError, "владельцу"):
            stats.collect("test-token")

    def test_metadata_absence_does_not_claim_complete_coverage(self):
        repo = {**REPOS[0], "pushed_at": None}
        with patch.object(stats, "api", side_effect=[({"login": "MrNo1ze"}, {}), ([repo], {}), ({}, {})]):
            result = stats.collect("test-token")
        self.assertFalse(result["coverage_verified"])
        self.assertEqual(result["without_languages"], 1)
        self.assertEqual(result["pushed_30d"], 0)

    def test_invalid_language_sizes_are_rejected(self):
        for size in (-1, "100", None, True):
            with self.subTest(size=size), patch.object(stats, "api", side_effect=[
                ({"login": "MrNo1ze"}, {}), ([REPOS[0]], {}), ({"Go": size}, {}),
            ]), self.assertRaisesRegex(ValueError, "размер"):
                stats.collect("test-token")

    def test_duplicate_foreign_and_public_repositories_are_rejected(self):
        variants = ([REPOS[0], REPOS[0]], [{**REPOS[0], "full_name": "other/secret"}],
                    [{**REPOS[0], "private": False}])
        for repos in variants:
            with self.subTest(repos=repos), patch.object(stats, "api", side_effect=[
                ({"login": "MrNo1ze"}, {}), (repos, {}), ({"Go": 1}, {}),
            ]), self.assertRaisesRegex(ValueError, "выборка"):
                stats.collect("test-token")

    def test_repeated_page_is_rejected(self):
        link = '<https://api.github.com/page2>; rel="next"'
        with patch.object(stats, "api", side_effect=[
            ({"login": "MrNo1ze"}, {}), ([REPOS[0]], {"link": link}), ([REPOS[1]], {"link": link}),
        ]), self.assertRaisesRegex(ValueError, "повторил"):
            stats.collect("test-token")

    def test_pagination_header_case_and_last_page(self):
        for key in ("Link", "link"):
            self.assertEqual(stats.next_link({key: '<https://api.github.com/page1>; rel="prev", <https://api.github.com/page3>; rel="next"'}),
                             "https://api.github.com/page3")
        self.assertIsNone(stats.next_link({"Link": '<https://api.github.com/page1>; rel="prev"'}))

    def test_external_pagination_host_cannot_receive_token(self):
        with patch("urllib.request.urlopen") as open_url, self.assertRaises(ValueError):
            stats.api("https://example.com/private", "test-token")
        open_url.assert_not_called()


class RenderTests(unittest.TestCase):
    def test_all_languages_and_small_shares_are_visible(self):
        svg = stats.render(FIXTURE)
        root = ET.fromstring(svg)
        texts = [element.text for element in root.findall(".//{*}text")]
        for name in FIXTURE["languages"]:
            self.assertIn(name, texts)
        self.assertNotIn("прочее", texts)
        self.assertIn("<0.01%", texts)
        self.assertIn("01:30:00 МСК", texts)
        self.assertIn("17.10.2026", svg)
        self.assertNotIn("secret-", svg)
        self.assertIn('textLength="270"', svg)

    def test_empty_and_many_languages_fit_dynamic_canvas(self):
        for count, segment_count in ((0, 0), (1, 1), (2, 2), (40, 1)):
            fixture = copy.deepcopy(FIXTURE)
            fixture["languages"] = {f"Language {i}": {"bytes": i + 1, "repositories": 1} for i in range(count)}
            svg = stats.render(fixture)
            root = ET.fromstring(svg)
            height = int(root.attrib["height"])
            for group in root.findall("{*}g"):
                transform = group.attrib.get("transform", "")
                if transform.startswith("translate("):
                    y = int(transform.removeprefix("translate(").removesuffix(")").split()[1])
                    self.assertLess(y + 43, height)
            segments = root.findall("{*}g[@clip-path]/{*}rect")
            self.assertEqual(len(segments), segment_count)
            if segments:
                last = segments[-1].attrib
                self.assertAlmostEqual(float(last["x"]) + float(last["width"]), 812, places=3)

    def test_bar_labels_separators_and_grouped_tail_keep_real_proportions(self):
        fixture = copy.deepcopy(FIXTURE)
        fixture["languages"] = {name: {"bytes": size, "repositories": 1} for name, size in
                                (("Go", 55), ("TypeScript", 15), ("JavaScript", 10),
                                 ("HTML", 8), ("Dart", 5), ("CSS", 4), ("Shell", 3))}
        root = ET.fromstring(stats.render(fixture))
        segments = root.findall("{*}g[@clip-path]/{*}rect")
        self.assertEqual(len(segments), 6)
        self.assertAlmostEqual(float(segments[-1].attrib["width"]), 784 * 0.07)
        self.assertEqual(segments[-1].find("{*}title").text, "Ещё 2 · 7.00%")
        for segment in segments:
            self.assertEqual(segment.attrib["stroke-width"], "2")
        labels = [text.text for text in root.findall('{*}text[@class="bar-label"]')]
        self.assertIn("Go", labels)
        self.assertIn("55.00%", labels)
        self.assertIn("Ещё 2", labels)
        self.assertNotEqual(stats.language_color("JavaScript"), stats.language_color("HTML"))

    def test_tiny_tail_is_not_enlarged_and_gets_external_label(self):
        root = ET.fromstring(stats.render(FIXTURE))
        tail = root.findall("{*}g[@clip-path]/{*}rect")[-1]
        self.assertLess(float(tail.attrib["width"]), 1)
        texts = [text.text for text in root.findall('{*}text[@class="muted"]')]
        self.assertIn("Ещё 14 · <0.01%", texts)

    def test_binary_units(self):
        for size, label in ((0, "0 B"), (1023, "1023 B"), (1024, "1.00 KiB"),
                            (1024**2, "1.00 MiB"), (1024**3, "1.00 GiB")):
            self.assertEqual(stats.human_bytes(size), label)


class PublishTests(unittest.TestCase):
    def setUp(self):
        args = patch("sys.argv", ["build_private_stats.py"])
        args.start()
        self.addCleanup(args.stop)
        summary = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""})
        summary.start()
        self.addCleanup(summary.stop)
        artifacts = Path.home() / "Downloads/ai-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="private-stats-test-", dir=artifacts)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "assets").mkdir()
        (self.root / "README.md").write_text('<img src="./assets/private-stats.svg"/>', encoding="utf-8")

    def test_repeated_calculation_changes_version_with_same_totals(self):
        fixture = copy.deepcopy(FIXTURE)
        stats.publish(fixture, self.root)
        first = (self.root / "README.md").read_text()
        stats.publish(fixture, self.root)
        self.assertEqual((self.root / "README.md").read_text(), first)
        fixture["generated_at"] = "2026-10-16T22:31:00+00:00"
        stats.publish(fixture, self.root)
        svg = (self.root / "assets/private-stats.svg").read_bytes()
        version = hashlib.sha256(svg).hexdigest()[:16]
        self.assertIn(f"?v={version}", (self.root / "README.md").read_text())
        self.assertNotEqual((self.root / "README.md").read_text(), first)
        self.assertEqual(json.loads((self.root / "assets/private-stats.json").read_text()), fixture)

    def test_broken_readme_prevents_writing_generated_files(self):
        (self.root / "README.md").write_text("no image", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ровно одна"):
            stats.publish(FIXTURE, self.root)
        self.assertEqual(list((self.root / "assets").iterdir()), [])

    def test_api_failure_keeps_previous_card(self):
        with patch.dict(os.environ, {"STATS_TOKEN": "test-token"}), patch.object(stats, "collect", side_effect=urllib.error.URLError("offline")), patch.object(stats, "publish") as publish:
            self.assertEqual(stats.main(), 1)
        publish.assert_not_called()

    def test_stats_pat_has_priority_over_workflow_token(self):
        with patch.dict(os.environ, {"STATS_TOKEN": "private-pat", "GITHUB_TOKEN": "workflow-token"}), patch.object(stats, "collect", return_value=FIXTURE) as collect, patch.object(stats, "publish"):
            self.assertEqual(stats.main(), 0)
        collect.assert_called_once_with("private-pat")

    def test_missing_token_stops_before_collection(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(stats, "collect") as collect:
            self.assertEqual(stats.main(), 1)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
