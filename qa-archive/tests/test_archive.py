import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qa-archive-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = {
            "action": "create",
            "title": "Git rebase \u51b2\u7a81",
            "category": "\u5de5\u4f5c/\u6280\u672f",
            "keywords": ["git", "rebase", "\u51b2\u7a81"],
            "lang": "ja",
            "question_html": "<p>What about &lt;script&gt; &amp; {{TITLE}}?</p>",
            "answer_html": "<pre><code>&lt;/script&gt; &amp; x &lt; y</code></pre>"
                           "<table><tbody><tr><td>Keep this</td></tr></tbody></table>",
        }

    def create(self):
        return Path(archive.write(self.root, self.data)["file"])

    def update_data(self, path, action="supplement"):
        data = {
            "action": action,
            "target": path.relative_to(self.root).as_posix(),
            "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "keywords": ["abort", "git"],
            "content_html": "<p>Use <code>git rebase --abort</code>.</p>",
        }
        if action == "correction":
            data["correction_note"] = "The earlier claim about abort is no longer valid."
        return data

    def index_entries(self):
        source = (self.root / "index.html").read_text(encoding="utf-8")
        return json.loads(re.search(r"const ENTRIES = (.*);\n", source)[1])

    def test_create_preserves_text_and_encodes_index(self):
        path = self.create()
        source = path.read_text(encoding="utf-8")
        self.assertIn('<html lang="ja">', source)
        self.assertIn('href="../../index.html"', source)
        self.assertIn("{{TITLE}}", source)
        entry = self.index_entries()[0]
        self.assertIn("</script> & x < y", entry["text"])
        self.assertNotIn("font-family", entry["text"])
        self.assertNotIn("\u8fd4\u56de", entry["text"])
        self.assertIn("%", entry["path"])
        self.assertEqual(entry["file"], path.relative_to(self.root).as_posix())
        self.assertEqual(entry["created_at"], entry["updated_at"])
        index = (self.root / "index.html").read_text(encoding="utf-8")
        self.assertIn("\\u003c/script\\u003e", index)
        self.assertEqual(index.count("</script>"), 1)

    def test_name_collision_does_not_overwrite(self):
        first = self.create()
        original = first.read_bytes()
        second = self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        self.assertTrue(second.stem.endswith("-2"))
        self.assertEqual(len(self.index_entries()), 2)

    def test_supplement_preserves_manual_edits_and_is_idempotent(self):
        path = self.create()
        source = path.read_text(encoding="utf-8").replace(
            "<table>", '<!-- manual formatting -->\n<table data-owner="human">')
        path.write_text(source, encoding="utf-8")
        original_body = re.search(b"<!-- manual formatting -->.*?</table>",
                                  path.read_bytes(), re.DOTALL)[0]
        data = self.update_data(path)
        self.assertEqual(archive.write(self.root, data)["status"], "supplement")
        updated = path.read_bytes()
        self.assertIn(original_body, updated)
        self.assertIn("abort", self.index_entries()[0]["keywords"])
        self.assertIn("git rebase --abort", self.index_entries()[0]["text"])
        self.assertEqual(archive.write(self.root, data)["status"], "skipped")
        self.assertEqual(path.read_bytes(), updated)

    def test_stale_update_refuses_to_overwrite(self):
        path = self.create()
        data = self.update_data(path)
        path.write_text(path.read_text(encoding="utf-8").replace("Keep this", "Manual edit"),
                        encoding="utf-8")
        changed = path.read_bytes()
        with self.assertRaisesRegex(archive.ArchiveError, "Entry changed"):
            archive.write(self.root, data)
        self.assertEqual(path.read_bytes(), changed)

    def test_correction_adds_visible_notice_and_preserves_answer(self):
        path = self.create()
        archive.write(self.root, self.update_data(path, "correction"))
        source = path.read_text(encoding="utf-8")
        self.assertIn("Keep this", source)
        self.assertLess(source.index("<aside"), source.index("<h1"))
        self.assertEqual(source.count("The earlier claim"), 2)
        self.assertIn("\u66f4\u6b63", source)

    def test_legacy_entry_scans_and_updates_without_reformatting(self):
        path = self.create()
        source = path.read_text(encoding="utf-8")
        source = re.sub(r'<meta name="qa-id"[^>]*>\n', "", source)
        source = re.sub(r'    <div><dt>[^<]*</dt><dd data-qa="updated_at">.*?</dd></div>\n', "", source)
        source = re.sub(r' data-qa="[^"]*"', "", source)
        path.write_text(source, encoding="utf-8")
        old = archive.entries(self.root)[0]
        self.assertEqual(old["created_at"], old["updated_at"])
        archive.write(self.root, self.update_data(path))
        new = archive.entries(self.root)[0]
        self.assertEqual(old["id"], new["id"])
        self.assertIn('<meta name="qa-id"', path.read_text(encoding="utf-8"))
        self.assertIn(self.data["answer_html"], path.read_text(encoding="utf-8"))

    def test_rebuild_uses_current_html_and_ignores_unmarked_files(self):
        path = self.create()
        path.write_text(path.read_text(encoding="utf-8").replace("Keep this", "Human changes"),
                        encoding="utf-8")
        (self.root / "unrelated.html").write_text("<not even valid", encoding="utf-8")
        result = archive.rebuild(self.root)
        self.assertEqual(result["count"], 1)
        self.assertIn("Human changes", self.index_entries()[0]["text"])

    def test_index_preserves_inline_words_and_direct_article_text(self):
        path = self.create()
        source = path.read_text(encoding="utf-8").replace(
            "</article>", "Direct human note<p>re<strong>base</strong></p>"
            "<p>Next paragraph</p></article>")
        path.write_text(source, encoding="utf-8")
        archive.rebuild(self.root)
        text = self.index_entries()[0]["text"]
        self.assertIn("Direct human note rebase Next paragraph", text)

    def test_unmarked_index_is_protected(self):
        index = self.root / "index.html"
        index.write_text("<html><body>Mine</body></html>", encoding="utf-8")
        with self.assertRaisesRegex(archive.ArchiveError, "unmarked index"):
            archive.write(self.root, self.data)
        self.assertEqual(index.read_text(encoding="utf-8"), "<html><body>Mine</body></html>")
        self.assertEqual(list(self.root.rglob("*.html")), [index])

    def test_malformed_entry_stops_rebuild_and_preserves_index(self):
        path = self.create()
        before = (self.root / "index.html").read_bytes()
        path.write_text("<!-- qa-archive:entry --><article></article>", encoding="utf-8")
        with self.assertRaisesRegex(archive.ArchiveError, re.escape(path.name)):
            archive.rebuild(self.root)
        self.assertEqual((self.root / "index.html").read_bytes(), before)

    def test_index_failure_reports_saved_entry_and_can_recover(self):
        with patch.object(archive, "rebuild", side_effect=OSError("index unavailable")):
            with self.assertRaisesRegex(archive.ArchiveError, "Entry is saved at"):
                archive.write(self.root, self.data)
        self.assertEqual(len(archive.entries(self.root)), 1)
        self.assertEqual(archive.rebuild(self.root)["count"], 1)

    def test_atomic_replace_failure_keeps_old_file_and_cleans_temp(self):
        target = self.root / "index.html"
        target.write_text("original", encoding="utf-8")
        with patch.object(archive.os, "replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                archive.publish(target, "replacement")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.glob(".qa-*.tmp")), [])

    def test_lock_excludes_another_writer_and_cleans_up(self):
        with archive.archive_lock(self.root):
            with self.assertRaises(FileExistsError):
                with archive.archive_lock(self.root):
                    self.fail("Second lock was acquired")
        self.assertFalse((self.root / ".qa-archive.lock").exists())

    def test_invalid_paths_and_input_fail_before_writing(self):
        for category in ["../escape", "A/B/C", "CON", "A/NUL.txt", "A.", "C:\\escape"]:
            with self.subTest(category=category):
                with self.assertRaises(archive.ArchiveError):
                    archive.write(self.root, dict(self.data, category=category))
        with self.assertRaisesRegex(archive.ArchiveError, "Unknown input"):
            archive.write(self.root, dict(self.data, typo="value"))
        self.assertEqual(list(self.root.rglob("*.html")), [])

    def test_body_whitelist_and_encoding(self):
        for fragment in [
            "<script>alert(1)</script>", '<p onclick="x">text</p>',
            '<a href="javascript:alert(1)">text</a>', '<a href="java&#10;script:x">x</a>',
            '<iframe>text</iframe>', '<a href="//example.com">x</a>',
            '<p style="color:red">x</p>', "<p>broken</div>",
        ]:
            with self.subTest(fragment=fragment):
                with self.assertRaises(archive.ArchiveError):
                    archive.safe_fragment(fragment)
        fragment = '<p><a href="https://example.com/?a=1&amp;b=2">A &amp; B</a></p>'
        self.assertEqual(archive.safe_fragment(fragment), fragment)
        self.assertEqual(archive.safe_fragment("<pre><code>&lt;script&gt;</code></pre>"),
                         "<pre><code>&lt;script&gt;</code></pre>")

    def test_duplicate_ids_stop_rebuild(self):
        path = self.create()
        path.with_name("copy.html").write_bytes(path.read_bytes())
        with self.assertRaisesRegex(archive.ArchiveError, "Duplicate entry IDs"):
            archive.rebuild(self.root)

    def test_scan_does_not_silently_ignore_unreadable_directories(self):
        def fail_walk(root, followlinks, onerror):
            onerror(PermissionError("unreadable category"))
            return iter(())

        with patch.object(archive.os, "walk", side_effect=fail_walk):
            with self.assertRaisesRegex(PermissionError, "unreadable category"):
                archive.entries(self.root)

    def test_update_date_is_refreshed_without_changing_creation(self):
        path = self.create()
        today = archive.date.today().isoformat()
        path.write_text(path.read_text(encoding="utf-8").replace(today, "2020-01-01"),
                        encoding="utf-8")
        archive.write(self.root, self.update_data(path))
        entry = self.index_entries()[0]
        self.assertEqual(entry["created_at"], "2020-01-01")
        self.assertEqual(entry["updated_at"], today)

    def test_one_level_category_and_reserved_characters_in_title(self):
        self.data["category"] = "Work"
        self.data["title"] = 'A/B:C?"<test> #100% &'
        path = self.create()
        self.assertEqual(path.parent, self.root / "Work")
        self.assertIn('href="../index.html"', path.read_text(encoding="utf-8"))
        entry = self.index_entries()[0]
        self.assertEqual(entry["title"], self.data["title"])
        self.assertIn("%23", entry["path"])
        self.assertIn("%25", entry["path"])

    def test_invalid_action_reports_validation_error(self):
        for value in (None, [], {}, 1, "merge"):
            with self.subTest(action=value):
                with self.assertRaises(archive.ArchiveError):
                    archive.write(self.root, {"action": value})

    def test_cli_write_scan_rebuild_and_error_exit(self):
        input_path = self.root / "input.json"
        input_path.write_text(json.dumps(self.data), encoding="utf-8")
        base = [sys.executable, "-B", str(archive.SKILL_ROOT / "scripts" / "archive.py")]
        for command, extra in [("write", ["--input", str(input_path)]), ("scan", []), ("rebuild", [])]:
            result = subprocess.run(base + [command, "--root", str(self.root)] + extra,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
        failed = subprocess.run(base + ["write", "--root", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stderr)["status"], "error")


if __name__ == "__main__":
    unittest.main()
