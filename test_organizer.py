import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

import Organizer


CONFIG: dict[str, Any] = {
    "categories": {
        "Audio": {"extensions": [".mp3", ".wav"], "mime_types": ["audio/*"]},
        "Image": {"extensions": [".jpg", ".png"], "mime_types": ["image/*"]},
        "PDF": {"extensions": [".pdf"], "mime_types": ["application/pdf"]},
    }
}


class RecordingReporter(Organizer.Reporter):
    def __init__(self, extensionless_response: bool = True) -> None:
        self.extensionless_response = extensionless_response
        self.warnings: list[tuple[str, str]] = []
        self.summary: Organizer.RunSummary | None = None

    def warning(self, title: str, message: str) -> None:
        self.warnings.append((title, message))

    def extensionless(self, filename: str) -> bool:
        self.warnings.append(("No extension", filename))
        return self.extensionless_response

    def complete(self, summary: Organizer.RunSummary) -> None:
        self.summary = summary


class OrganizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary_directory.name)
        self.write_config()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_config(self, data: dict[str, Any] = CONFIG) -> None:
        (self.target / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def write_state(self, sequences: dict[str, int]) -> None:
        (self.target / Organizer.STATE_NAME).write_text(
            json.dumps({"version": 1, "sequences": sequences}), encoding="utf-8"
        )

    @staticmethod
    def require_summary(summary: Organizer.RunSummary | None) -> Organizer.RunSummary:
        if summary is None:
            raise AssertionError("Expected an organizer summary.")
        return summary

    def run_organizer(
        self, reporter: RecordingReporter | None = None
    ) -> tuple[int, Organizer.RunSummary | None, RecordingReporter]:
        chosen_reporter = reporter if reporter is not None else RecordingReporter()
        code, summary = Organizer.run(self.target, chosen_reporter)
        return code, summary, chosen_reporter

    def test_normal_move_is_sorted_and_preserves_extensions(self):
        (self.target / "b.WAV").write_text("b", encoding="utf-8")
        (self.target / "A.mp3").write_text("a", encoding="utf-8")
        (self.target / "image.PNG").write_text("image", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 0)
        self.assertEqual((self.require_summary(summary).moved, self.require_summary(summary).skipped, self.require_summary(summary).failed), (3, 0, 0))
        self.assertEqual(sorted(path.name for path in (self.target / "Audio").iterdir()), ["Audio_1.mp3", "Audio_2.WAV"])
        self.assertEqual(sorted(path.name for path in (self.target / "Image").iterdir()), ["Image_1.PNG"])
        state = json.loads((self.target / Organizer.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["sequences"], {"Audio": 3, "Image": 2})

    def test_repeat_run_only_processes_new_root_files(self):
        (self.target / "first.mp3").write_text("first", encoding="utf-8")
        self.assertEqual(self.run_organizer()[0], 0)
        (self.target / "second.wav").write_text("second", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 0)
        self.assertEqual(self.require_summary(summary).moved, 1)
        self.assertEqual(sorted(path.name for path in (self.target / "Audio").iterdir()), ["Audio_1.mp3", "Audio_2.wav"])

    def test_root_folder_and_filename_spaces_and_unicode_are_supported(self):
        source = self.target / "été song.mp3"
        source.write_text("audio", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())

    def test_target_path_with_spaces_is_supported(self):
        spaced_target = self.target / "folder with spaces"
        spaced_target.mkdir()
        (spaced_target / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        (spaced_target / "song.mp3").write_text("audio", encoding="utf-8")

        code, summary = Organizer.run(spaced_target, RecordingReporter())

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((spaced_target / "Audio" / "Audio_1.mp3").exists())

    def test_numeric_slot_collisions_are_extension_independent(self):
        category = self.target / "Audio"
        category.mkdir()
        (category / "Audio_1.txt").write_text("manual", encoding="utf-8")
        (category / "audio_2").write_text("manual", encoding="utf-8")
        self.write_state({"Audio": 1})
        (self.target / "song.wav").write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 0)
        self.assertEqual(self.require_summary(summary).moved, 1)
        self.assertTrue((category / "Audio_3.wav").exists())
        self.assertEqual(json.loads((self.target / Organizer.STATE_NAME).read_text())["sequences"]["Audio"], 4)

    def test_high_serial_file_does_not_override_authoritative_state(self):
        category = self.target / "Audio"
        category.mkdir()
        (category / "Audio_100.mp3").write_text("manual", encoding="utf-8")
        self.write_state({"Audio": 3})
        (self.target / "song.mp3").write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((category / "Audio_3.mp3").exists())
        self.assertFalse((category / "Audio_101.mp3").exists())

    def test_existing_category_folder_is_reused_case_insensitively(self):
        category = self.target / "audio"
        category.mkdir()
        self.write_state({"Audio": 1})
        (self.target / "song.mp3").write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((category / "Audio_1.mp3").exists())
        matching_directories = [
            path.name for path in self.target.iterdir() if path.is_dir() and path.name.casefold() == "audio"
        ]
        self.assertEqual(matching_directories, ["audio"])

    def test_manual_category_contents_are_not_adopted(self):
        category = self.target / "Audio"
        category.mkdir()
        manual = category / "manually_added.mp3"
        manual.write_text("manual", encoding="utf-8")
        self.write_state({"Audio": 1})
        (self.target / "new.mp3").write_text("new", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertEqual(manual.read_text(encoding="utf-8"), "manual")
        self.assertTrue((category / "Audio_1.mp3").exists())

    def test_invalid_config_aborts_before_any_move(self):
        self.write_config(
            {"categories": {"Audio": {"extensions": [".mp3"], "mime_types": []}, "Music": {"extensions": [".MP3"], "mime_types": []}}}
        )
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")

        code, summary, reporter = self.run_organizer()

        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())
        self.assertFalse((self.target / Organizer.STATE_NAME).exists())
        self.assertIn("Organization aborted", [title for title, _ in reporter.warnings])

    def test_unsafe_and_ambiguous_rules_are_rejected(self):
        with self.assertRaises(Organizer.ConfigError):
            Organizer.validate_config({"categories": {"../escape": {"extensions": [], "mime_types": []}}})
        with self.assertRaises(Organizer.ConfigError):
            Organizer.validate_config({"categories": {"Image": {"extensions": [], "mime_types": ["image/*"]}, "Photo": {"extensions": [], "mime_types": ["image/jpeg"]}}})
        with self.assertRaises(Organizer.ConfigError):
            Organizer.validate_config({"categories": {"Unknown": {"extensions": [], "mime_types": []}}})
        with self.assertRaises(Organizer.ConfigError):
            Organizer.validate_config({"categories": {"Audio": {"extensions": [".mp3", ".mp3"], "mime_types": []}}})
        with self.assertRaises(Organizer.ConfigError):
            Organizer.validate_config({"categories": {"Archive": {"extensions": [".tar.gz"], "mime_types": []}}})

    def test_category_path_file_conflict_aborts_before_move(self):
        (self.target / "Audio").write_text("not a directory", encoding="utf-8")
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())

    def test_first_run_reuses_existing_empty_category(self):
        (self.target / "Audio").mkdir()
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())

    def test_missing_state_after_established_run_aborts(self):
        (self.target / Organizer.LOCK_NAME).write_bytes(Organizer.ESTABLISHED_MARKER)
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())

    def test_failed_first_attempt_does_not_establish_state_marker(self):
        self.write_config({"invalid": {}})
        (self.target / "Audio").mkdir()
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertEqual((self.target / Organizer.LOCK_NAME).read_bytes(), b"0")

        self.write_config()
        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())

    def test_corrupt_and_boolean_state_values_abort(self):
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")
        (self.target / Organizer.STATE_NAME).write_text('{"version": 1, "sequences": {"Audio": true}}', encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())

    def test_recognized_extension_wins_over_mime(self):
        source = self.target / "sound.mp3"
        source.write_text("sound", encoding="utf-8")
        with mock.patch.object(Organizer.mimetypes, "guess_type", return_value=("image/jpeg", None)) as guessed:
            code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        guessed.assert_not_called()
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())

    def test_unknown_extension_uses_mime_then_unknown_fallback(self):
        (self.target / "likely_audio.custom").write_text("audio", encoding="utf-8")
        (self.target / "mystery.xyz").write_text("unknown", encoding="utf-8")

        def guess(name: str, strict: bool = False) -> tuple[str | None, str | None]:
            return ("audio/x-test", None) if name.startswith("likely") else (None, None)

        with mock.patch.object(Organizer.mimetypes, "guess_type", side_effect=guess):
            code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 2))
        self.assertTrue((self.target / "Audio" / "Audio_1.custom").exists())
        self.assertTrue((self.target / "Unknown" / "Unknown_1.xyz").exists())

    def test_extensionless_continue_and_skip_are_distinct(self):
        extensionless = self.target / "without_extension"
        extensionless.write_text("data", encoding="utf-8")
        with mock.patch.object(Organizer.mimetypes, "guess_type", return_value=(None, None)):
            code, summary, reporter = self.run_organizer(RecordingReporter(extensionless_response=True))

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "No Extension" / "No Extension_1").exists())
        self.assertIn(("No extension", "without_extension"), reporter.warnings)

        remaining = self.target / "skip_this"
        remaining.write_text("data", encoding="utf-8")
        code, summary, _ = self.run_organizer(RecordingReporter(extensionless_response=False))
        self.assertEqual((code, self.require_summary(summary).skipped), (0, 1))
        self.assertTrue(remaining.exists())

    def test_hidden_and_infrastructure_files_have_required_behavior(self):
        hidden = self.target / ".hidden.mp3"
        hidden.write_text("audio", encoding="utf-8")
        for name in ("Organizer.py", "Organize Files.bat"):
            (self.target / name).write_text("protected", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())
        for name in ("Organizer.py", "Organize Files.bat"):
            self.assertTrue((self.target / name).exists())

    def test_state_temp_files_are_protected_but_normal_tmp_files_are_processed(self):
        state_temp = self.target / ".organizer_state_deadbeef.tmp"
        normal_tmp = self.target / "normal.tmp"
        state_temp.write_text("internal", encoding="utf-8")
        normal_tmp.write_text("user", encoding="utf-8")
        config: dict[str, Any] = {
            "categories": {
                "Temp": {"extensions": [".tmp"], "mime_types": []},
            }
        }
        self.write_config(config)

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue(state_temp.exists())
        self.assertTrue((self.target / "Temp" / "Temp_1.tmp").exists())

    def test_cleanup_failure_is_reported_without_aborting_run(self):
        reporter = RecordingReporter()
        category = self.target / "Audio"
        category.mkdir()
        with mock.patch.object(Organizer.Path, "rmdir", side_effect=OSError("simulated failure")):
            Organizer.clean_empty_directories({category}, reporter)

        self.assertTrue(category.exists())
        self.assertIn("Cleanup failed", [title for title, _ in reporter.warnings])

    def test_non_empty_new_category_is_not_cleaned_or_reported(self):
        (self.target / "song.mp3").write_text("song", encoding="utf-8")

        code, summary, reporter = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())
        self.assertNotIn("Cleanup failed", [title for title, _ in reporter.warnings])

    def test_shortcut_is_skipped_with_a_warning(self):
        shortcut = self.target / "external.lnk"
        shortcut.write_text("not followed", encoding="utf-8")

        code, summary, reporter = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).skipped), (0, 1))
        self.assertTrue(shortcut.exists())
        self.assertIn("Link skipped", [title for title, _ in reporter.warnings])

    def test_symbolic_link_is_skipped_without_following_its_target(self):
        with tempfile.TemporaryDirectory() as external_directory:
            outside = Path(external_directory) / "outside.mp3"
            outside.write_text("outside", encoding="utf-8")
            link = self.target / "linked.mp3"
            try:
                os.symlink(outside, link)
            except OSError as error:
                self.skipTest(f"Symbolic links are unavailable in this test environment: {error}")

            code, summary, reporter = self.run_organizer()

            self.assertEqual((code, self.require_summary(summary).skipped), (0, 1))
            self.assertTrue(link.is_symlink())
            self.assertTrue(outside.exists())
            self.assertIn("Link skipped", [title for title, _ in reporter.warnings])

    def test_files_created_after_snapshot_wait_for_next_run(self):
        first = self.target / "first.mp3"
        later = self.target / "later.mp3"
        first.write_text("first", encoding="utf-8")
        original_classify = Organizer.classify

        def classify_and_create(
            path: Path, rules: Organizer.ClassificationRules, reporter: Organizer.Reporter
        ) -> str | None:
            if not later.exists():
                later.write_text("later", encoding="utf-8")
            return original_classify(path, rules, reporter)

        with mock.patch.object(Organizer, "classify", side_effect=classify_and_create):
            code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue(later.exists())
        self.assertEqual(self.require_summary(self.run_organizer()[1]).moved, 1)

    def test_disappearing_file_is_skipped(self):
        first = self.target / "a.mp3"
        vanishing = self.target / "b.mp3"
        first.write_text("first", encoding="utf-8")
        vanishing.write_text("second", encoding="utf-8")
        original_classify = Organizer.classify

        def classify_and_delete(
            path: Path, rules: Organizer.ClassificationRules, reporter: Organizer.Reporter
        ) -> str | None:
            if path.name == "a.mp3":
                vanishing.unlink()
            return original_classify(path, rules, reporter)

        with mock.patch.object(Organizer, "classify", side_effect=classify_and_delete):
            code, summary, reporter = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved, self.require_summary(summary).skipped), (0, 1, 1))
        self.assertIn("File skipped", [title for title, _ in reporter.warnings])

    def test_move_failure_consumes_number_and_remaining_files_continue(self):
        bad = self.target / "a.mp3"
        good = self.target / "b.mp3"
        bad.write_text("bad", encoding="utf-8")
        good.write_text("good", encoding="utf-8")
        original_rename = Organizer.os.rename

        def failing_rename(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            if Path(source).name == "a.mp3":
                raise PermissionError("simulated lock")
            return original_rename(source, destination)

        with mock.patch.object(Organizer.os, "rename", side_effect=failing_rename):
            code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved, self.require_summary(summary).failed), (0, 1, 1))
        self.assertTrue(bad.exists())
        self.assertTrue((self.target / "Audio" / "Audio_2.mp3").exists())
        state = json.loads((self.target / Organizer.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["sequences"]["Audio"], 3)

    def test_new_empty_category_is_cleaned_after_state_save_failure(self):
        self.write_state({})
        (self.target / "song.mp3").write_text("song", encoding="utf-8")
        with mock.patch.object(Organizer, "persist_state", side_effect=Organizer.StateError("disk full")):
            code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).failed), (1, 1))
        self.assertFalse((self.target / "Audio").exists())
        self.assertTrue((self.target / "song.mp3").exists())

    def test_invalid_utf8_and_boolean_state_version_abort(self):
        source = self.target / "song.mp3"
        source.write_text("song", encoding="utf-8")
        (self.target / Organizer.CONFIG_NAME).write_bytes(b"\xff")
        code, summary, _ = self.run_organizer()
        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())

        self.write_config()
        (self.target / Organizer.STATE_NAME).write_text(
            '{"version": true, "sequences": {}}', encoding="utf-8"
        )
        code, summary, _ = self.run_organizer()
        self.assertEqual(code, 1)
        self.assertIsNone(summary)
        self.assertTrue(source.exists())

    def test_rename_false_preserves_original_filename(self):
        config: dict[str, Any] = {
            "categories": {
                "Programming & System": {
                    "rename": False,
                    "extensions": [".py", ".exe"],
                    "mime_types": [],
                }
            }
        }
        self.write_config(config)
        source = self.target / "my_script.py"
        source.write_text("print('hello')", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 1))
        self.assertTrue((self.target / "Programming & System" / "my_script.py").exists())
        self.assertFalse(source.exists())
        state = json.loads((self.target / Organizer.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["sequences"], {})

    def test_rename_false_does_not_consume_sequence_numbers(self):
        config: dict[str, Any] = {
            "categories": {
                "Audio": {
                    "rename": True,
                    "extensions": [".mp3"],
                    "mime_types": [],
                },
                "Programming & System": {
                    "rename": False,
                    "extensions": [".py"],
                    "mime_types": [],
                },
            }
        }
        self.write_config(config)
        (self.target / "script.py").write_text("print(1)", encoding="utf-8")
        (self.target / "song.mp3").write_text("audio", encoding="utf-8")

        code, summary, _ = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).moved), (0, 2))
        self.assertTrue((self.target / "Programming & System" / "script.py").exists())
        self.assertTrue((self.target / "Audio" / "Audio_1.mp3").exists())
        state = json.loads((self.target / Organizer.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["sequences"], {"Audio": 2})

    def test_rename_false_collision_skips_without_overwriting(self):
        config: dict[str, Any] = {
            "categories": {
                "Programming & System": {
                    "rename": False,
                    "extensions": [".py"],
                    "mime_types": [],
                }
            }
        }
        self.write_config(config)
        destination_directory = self.target / "Programming & System"
        destination_directory.mkdir()
        existing = destination_directory / "script.py"
        existing.write_text("original", encoding="utf-8")
        source = self.target / "script.py"
        source.write_text("new", encoding="utf-8")

        code, summary, reporter = self.run_organizer()

        self.assertEqual((code, self.require_summary(summary).skipped), (0, 1))
        self.assertTrue(source.exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "original")
        self.assertIn("File move skipped", [title for title, _ in reporter.warnings])

    def test_invalid_rename_values_are_rejected(self):
        invalid_values: tuple[object, ...] = ("false", 0, 1, None, [])
        for invalid_value in invalid_values:
            with self.subTest(rename=invalid_value):
                with self.assertRaises(Organizer.ConfigError):
                    Organizer.validate_config(
                        {
                            "categories": {
                                "Programming": {
                                    "rename": invalid_value,
                                    "extensions": [".py"],
                                    "mime_types": [],
                                }
                            }
                        }
                    )

    def test_rename_defaults_to_true_when_omitted(self):
        rules = Organizer.validate_config(
            {
                "categories": {
                    "Audio": {
                        "extensions": [".mp3"],
                        "mime_types": [],
                    }
                }
            }
        )
        self.assertTrue(rules.rename_by_category["Audio"])

    def test_lock_rejects_a_second_process(self):
        ready = self.target / "ready"
        organizer_directory = Path(Organizer.__file__).resolve().parent
        script = "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(organizer_directory)!r})",
                "from Organizer import OrganizerLock, LOCK_NAME",
                f"target = Path({str(self.target)!r})",
                "with OrganizerLock(target / LOCK_NAME):",
                f"    Path({str(ready)!r}).write_text('ready')",
                "    time.sleep(3)",
            ]
        )
        process = subprocess.Popen([sys.executable, "-c", script])
        try:
            for _ in range(30):
                if ready.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(ready.exists(), "lock-holder process did not start")
            code, summary, _ = self.run_organizer()
            self.assertEqual(code, 1)
            self.assertIsNone(summary)
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
