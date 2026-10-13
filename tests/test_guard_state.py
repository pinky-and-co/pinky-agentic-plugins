#!/usr/bin/env python3
"""Tests for the guard's per-session state — `hooks/guard-state/` of pc-kanban:
`pc_kanban_guard_state.py` (the file) and `pc_kanban_guard_session_hook.py` (the DirectoryAdded
and SessionEnd hook). Run with: python3 -m pytest tests/test_guard_state.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

_STATE_DIR = (
    Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks" / "guard-state"
)
sys.path.insert(0, str(_STATE_DIR))

import pc_kanban_guard_state as gs  # noqa: E402

HOOK = str(_STATE_DIR / "pc_kanban_guard_session_hook.py")
A, B = "session-a", "session-b"


def _stamp(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime(gs.UPDATED_FORMAT)


class StateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.path = gs.state_path(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def table(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))["sessions"]

    def fire(self, event: dict, **env) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"cwd": str(self.root), **event}),
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), **env},
            check=False,
        )

    def add_dir(self, session_id: str, directory: str) -> subprocess.CompletedProcess:
        return self.fire(
            {
                "hook_event_name": "DirectoryAdded",
                "session_id": session_id,
                "directory": directory,
                "source": "slash_command",
            }
        )


class TestDirectoryAddedHook(StateCase):
    def test_fr_0025_377_adds_without_dropping_or_duplicating(self):
        first, second = str(self.root / "one"), str(self.root / "two")
        for directory in (first, second, first):
            result = self.add_dir(A, directory)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.table()[A]["added_dirs"], [first, second])

    def test_added_dirs_are_per_session(self):
        self.add_dir(A, str(self.root / "one"))
        self.add_dir(B, str(self.root / "two"))
        self.assertEqual(self.table()[A]["added_dirs"], [str(self.root / "one")])
        self.assertEqual(self.table()[B]["added_dirs"], [str(self.root / "two")])

    def test_keeps_the_sessions_freeze_when_a_directory_is_added(self):
        gs.update_session(
            self.path, A, lambda e: e.update(freeze=["src"], careful=True)
        )
        self.add_dir(A, str(self.root / "one"))
        entry = gs.load_session(self.path, A)
        self.assertEqual(
            (entry["careful"], entry["freeze"], entry["added_dirs"]),
            (True, ["src"], [str(self.root / "one")]),
        )

    def test_a_relative_directory_resolves_against_the_event_cwd(self):
        self.add_dir(A, "../sibling")
        self.assertEqual(
            self.table()[A]["added_dirs"],
            [os.path.normpath(self.root / ".." / "sibling")],
        )

    def test_an_event_missing_a_field_names_that_field_only_and_its_keys(self):
        cases = (
            (
                {"hook_event_name": "DirectoryAdded", "directory": "/x"},
                "session_id",
                "directory",
            ),
            (
                {"hook_event_name": "DirectoryAdded", "session_id": A},
                "directory",
                "session_id",
            ),
            (
                {"hook_event_name": "DirectoryAdded", "session_id": A, "directory": ""},
                "directory",
                "session_id",
            ),
        )
        for event, missing, present in cases:
            with self.subTest(event=event):
                result = self.fire(event)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                (line,) = result.stderr.strip().splitlines()
                named, _, keys = line.partition("event keys")
                self.assertIn(missing, named)
                self.assertNotIn(present, named)
                self.assertIn(present, keys)
        self.assertFalse(self.path.exists())

    def test_an_event_missing_both_fields_names_both(self):
        result = self.fire({"hook_event_name": "DirectoryAdded"})
        named = result.stderr.partition("event keys")[0]
        self.assertEqual(result.returncode, 1)
        self.assertIn("session_id and directory", named)

    def test_a_garbled_stdin_exits_zero(self):
        result = subprocess.run(
            [sys.executable, HOOK],
            input="{not json",
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)},
            check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_concurrent_additions_are_all_kept(self):
        dirs = [str(self.root / f"d{i}") for i in range(12)]
        procs = [
            subprocess.Popen(
                [sys.executable, HOOK],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)},
            )
            for _ in dirs
        ]
        for proc, directory in zip(procs, dirs):
            assert proc.stdin is not None
            proc.stdin.write(
                json.dumps(
                    {
                        "hook_event_name": "DirectoryAdded",
                        "session_id": A,
                        "directory": directory,
                        "cwd": str(self.root),
                    }
                )
            )
            proc.stdin.close()
        for proc in procs:
            proc.wait(timeout=30)
        self.assertEqual(sorted(self.table()[A]["added_dirs"]), sorted(dirs))


class TestSessionEnd(StateCase):
    def test_fr_0025_380_drops_the_ending_session_and_keeps_the_others(self):
        gs.update_session(self.path, A, lambda e: e.update(freeze=["a"]))
        gs.update_session(self.path, B, lambda e: e.update(freeze=["b"]))
        before = self.table()[B]
        result = self.fire({"hook_event_name": "SessionEnd", "session_id": A})
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertNotIn(A, self.table())
        self.assertEqual(self.table()[B], before)

    def test_a_session_end_without_a_session_id_names_its_keys_and_exits_one(self):
        for event in (
            {"hook_event_name": "SessionEnd", "reason": "clear"},
            {"hook_event_name": "SessionEnd", "session_id": "", "reason": "clear"},
        ):
            with self.subTest(event=event):
                result = self.fire(event)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn("event keys", result.stderr)
                self.assertIn("reason", result.stderr)
        self.assertFalse(self.path.exists())

    def test_ending_a_session_without_an_entry_does_not_create_the_file(self):
        self.fire({"hook_event_name": "SessionEnd", "session_id": A})
        self.assertFalse(self.path.exists())


class TestStateFile(StateCase):
    def test_fr_0025_381_a_write_drops_entries_older_than_seven_days(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "sessions": {
                        "old": {"careful": True, "updated": _stamp(8)},
                        "recent": {"careful": True, "updated": _stamp(6)},
                        "unstamped": {"careful": True},
                    }
                }
            ),
            encoding="utf-8",
        )
        gs.update_session(self.path, A, lambda e: e.update(careful=True))
        self.assertEqual(sorted(self.table()), ["recent", A])

    def test_update_stamps_the_entry_and_returns_it(self):
        entry = gs.update_session(self.path, A, lambda e: e["freeze"].append("src"))
        self.assertEqual(entry["freeze"], ["src"])
        self.assertEqual(self.table()[A]["updated"][:10], _stamp(0)[:10])

    def test_unknown_keys_and_wrong_types_are_dropped_on_write(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"sessions": {A: {"freeze": "src", "deny": [1], "x": 1}}}),
            encoding="utf-8",
        )
        gs.update_session(self.path, A, lambda _entry: None)
        entry = self.table()[A]
        self.assertEqual(
            {k: entry[k] for k in ("careful", "freeze", "deny", "added_dirs")},
            {"careful": False, "freeze": [], "deny": ["1"], "added_dirs": []},
        )
        self.assertNotIn("x", entry)

    def test_a_flat_format_file_is_kept_as_legacy_by_the_first_write(self):
        flat = json.dumps({"careful": True, "freeze": ["src"], "deny": []})
        self.path.parent.mkdir(parents=True)
        self.path.write_text(flat, encoding="utf-8")
        legacy = self.path.with_name(self.path.name + ".legacy")
        gs.update_session(self.path, A, lambda e: e.update(careful=True))
        self.assertEqual(legacy.read_text(encoding="utf-8"), flat)
        self.assertEqual(list(self.table()), [A])

    def test_an_existing_legacy_copy_is_not_overwritten(self):
        legacy = self.path.with_name(self.path.name + ".legacy")
        self.path.parent.mkdir(parents=True)
        legacy.write_text("first copy", encoding="utf-8")
        self.path.write_text(json.dumps({"careful": True}), encoding="utf-8")
        gs.update_session(self.path, A, lambda e: None)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "first copy")

    def test_a_current_or_corrupt_file_leaves_no_legacy_copy(self):
        legacy = self.path.with_name(self.path.name + ".legacy")
        gs.update_session(self.path, A, lambda e: None)
        gs.update_session(self.path, B, lambda e: None)
        self.assertFalse(legacy.exists())
        self.path.write_text("{not json", encoding="utf-8")
        gs.update_session(self.path, A, lambda e: None)
        self.assertFalse(legacy.exists())

    def test_load_session_without_a_session_id_is_empty(self):
        gs.update_session(self.path, A, lambda e: e.update(careful=True))
        for session_id in (None, "", 7):
            self.assertEqual(
                gs.load_session(self.path, session_id),
                {"careful": False, "freeze": [], "deny": [], "added_dirs": []},
            )

    def test_pc_guard_state_env_overrides_the_location(self):
        target = self.root / "elsewhere.json"
        old = os.environ.get(gs.STATE_ENV)
        os.environ[gs.STATE_ENV] = str(target)
        try:
            self.assertEqual(gs.state_path(self.root), target)
        finally:
            if old is None:
                del os.environ[gs.STATE_ENV]
            else:
                os.environ[gs.STATE_ENV] = old


@unittest.skipUnless(gs.HAS_FLOCK, "no fcntl on this platform")
class TestLock(StateCase):
    def test_the_lock_is_real_a_second_exclusive_flock_is_refused(self):
        import fcntl

        self.path.parent.mkdir(parents=True)
        with gs._locked(self.path):
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again after the block
        finally:
            os.close(fd)

    def _stderr_of_a_write(self) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gs.update_session(self.path, A, lambda e: e.update(careful=True))
        return err.getvalue()

    def test_a_refused_lock_is_reported_on_stderr_and_the_write_still_happens(self):
        with unittest.mock.patch.object(gs.fcntl, "flock", side_effect=OSError("boom")):
            message = self._stderr_of_a_write()
        self.assertEqual(len(message.strip().splitlines()), 1)
        self.assertIn("lock not acquired", message)
        self.assertTrue(gs.load_session(self.path, A)["careful"])

    def test_a_missing_fcntl_is_reported_on_stderr_and_the_write_still_happens(self):
        with unittest.mock.patch.object(gs, "fcntl", None):
            message = self._stderr_of_a_write()
        self.assertEqual(len(message.strip().splitlines()), 1)
        self.assertIn("fcntl unavailable", message)
        self.assertTrue(gs.load_session(self.path, A)["careful"])

    def test_a_granted_lock_prints_nothing(self):
        self.assertEqual(self._stderr_of_a_write(), "")


if __name__ == "__main__":
    unittest.main()
