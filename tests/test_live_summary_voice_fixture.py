"""Offline fixtures: all subprocess execution is mocked; no sound or models."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from teams_transcription.live_summary_voice_fixture import (
    MAX_PCM_BYTES, UTTERANCE, VOICE_TEXT, describe_voice_pcm, load_voice_fixture, prepare_voice_fixture)

MODULE = "teams_transcription.live_summary_voice_fixture."
PCM = b"\x01\x00\x00\x00\xff\xff"
WAV = b"RIFF-controlled-wav"
REAL_SUBPROCESS_RUN = subprocess.run


def process(stdout, returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode,
                           stderr=b"private-token secret-SDP meeting-url")


class LiveSummaryVoiceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.path = self.parent / "voice.pcm"
        self.runner = patch(MODULE + "subprocess.run")
        self.run = self.runner.start()
        self.addCleanup(self.runner.stop)
        self.run.side_effect = [process(WAV), process(PCM)]

    def assert_code(self, code, operation):
        with self.assertRaises(ValueError) as caught:
            operation()
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn("private", str(caught.exception))

    def test_exact_phrase_absolute_binaries_wav_conversion_and_metadata_only(self):
        metadata = prepare_voice_fixture(self.path)
        self.assertEqual(UTTERANCE,
            "Please summarize the supplied meeting notes and return the requested JSON and completion marker.")
        self.assertEqual(VOICE_TEXT, UTTERANCE)
        first, second = self.run.call_args_list
        self.assertEqual(first.args[0], ["/usr/bin/espeak-ng", "--stdout", "-v", "en-us", "-s", "160", UTTERANCE])
        self.assertIsNone(first.kwargs["input"])
        self.assertEqual(second.args[0], ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"])
        self.assertEqual(second.kwargs["input"], WAV)
        for call in (first, second):
            self.assertTrue(call.kwargs["capture_output"])
            self.assertFalse(call.kwargs["check"])
            self.assertNotIn("shell", call.kwargs)
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 15)
        self.assertEqual(metadata, {"sample_rate": 24000, "channels": 1, "format": "pcm16le",
            "bytes": 6, "frames": 3, "duration_ms": .125, "nonzero_frames": 2,
            "sha256": hashlib.sha256(PCM).hexdigest()})
        self.assertEqual(self.path.read_bytes(), PCM)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.parent.iterdir()), [self.path])

    def test_one_shared_deadline_not_two_fifteen_second_budgets(self):
        with patch(MODULE + "time.monotonic", side_effect=[100, 100, 107, 108]):
            prepare_voice_fixture(self.path)
        self.assertEqual([c.kwargs["timeout"] for c in self.run.call_args_list], [15, 8])

    def test_expired_budget_before_second_process_does_not_create_artifact(self):
        with patch(MODULE + "time.monotonic", side_effect=[100, 100, 115]):
            self.assert_code("voice_fixture_timeout", lambda: prepare_voice_fixture(self.path))
        self.assertEqual(self.run.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_timeout_from_either_run_is_fixed_and_never_writes(self):
        for stage in (0, 1):
            with self.subTest(stage=stage):
                self.run.reset_mock()
                error = subprocess.TimeoutExpired("private command", 3,
                    output=b"private source", stderr=b"private stderr")
                self.run.side_effect = [error] if stage == 0 else [process(WAV), error]
                self.assert_code("voice_fixture_timeout", lambda: prepare_voice_fixture(self.path))
                self.assertEqual(self.run.call_count, stage + 1)
                self.assertFalse(self.path.exists())

    def test_stdlib_timeout_kills_and_waits_for_mocked_child_without_executing_binary(self):
        child = MagicMock()
        child.__enter__.return_value = child
        child.communicate.side_effect = subprocess.TimeoutExpired("private command", 3,
            output=b"private source", stderr=b"private stderr")
        self.run.side_effect = REAL_SUBPROCESS_RUN
        with patch(MODULE + "subprocess.Popen", return_value=child) as popen:
            self.assert_code("voice_fixture_timeout", lambda: prepare_voice_fixture(self.path))
        popen.assert_called_once()
        child.kill.assert_called_once()
        child.wait.assert_called_once()
        child.__exit__.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_deadline_expiring_during_conversion_prevents_file_creation(self):
        with patch(MODULE + "time.monotonic", side_effect=[100, 100, 101, 116]):
            self.assert_code("voice_fixture_timeout", lambda: prepare_voice_fixture(self.path))
        self.assertEqual(self.run.call_count, 2)
        self.assertFalse(self.path.exists())

    def test_arbitrary_process_exception_or_malformed_result_never_exposes_payload(self):
        for error in (RuntimeError("private-token stdout stderr"),
                      ValueError("private-token stdout stderr"), object(), process(WAV, True)):
            with self.subTest(kind=type(error).__name__):
                self.run.side_effect = [error]
                self.assert_code("voice_fixture_synthesis_failed", lambda: prepare_voice_fixture(self.path))
                self.assertFalse(self.path.exists())

    def test_failed_binary_or_missing_binary_has_fixed_error_and_no_file(self):
        cases = [([process(WAV, 1)], "voice_fixture_synthesis_failed"),
                 ([OSError("private path")], "voice_fixture_synthesis_failed"),
                 ([process(WAV), process(PCM, 1)], "voice_fixture_conversion_failed"),
                 ([process(WAV), OSError("private path")], "voice_fixture_conversion_failed")]
        for effect, code in cases:
            with self.subTest(code=code, effect=type(effect[-1]).__name__):
                self.run.side_effect = effect
                self.assert_code(code, lambda: prepare_voice_fixture(self.path))
                self.assertFalse(self.path.exists())

    def test_bounded_wav_before_conversion_or_creation(self):
        for invalid in (b"", b"x" * 1048577, bytearray(WAV)):
            with self.subTest(size=len(invalid)):
                self.run.reset_mock()
                self.run.side_effect = [process(invalid)]
                self.assert_code("voice_fixture_wav_invalid", lambda: prepare_voice_fixture(self.path))
                self.assertEqual(self.run.call_count, 1)
                self.assertFalse(self.path.exists())

    def test_conversion_shape_size_and_silence_rejected_before_file_creation(self):
        for invalid in (b"", b"x", b"\x00\x00", b"x" * (MAX_PCM_BYTES + 2), bytearray(PCM)):
            with self.subTest(size=len(invalid)):
                self.run.side_effect = [process(WAV), process(invalid)]
                self.assert_code("voice_fixture_pcm_invalid", lambda: prepare_voice_fixture(self.path))
                self.assertFalse(self.path.exists())

    def test_ten_second_pcm_upper_boundary_and_next_frame(self):
        maximum = b"\x01\x00" * 240000
        self.run.side_effect = [process(WAV), process(maximum)]
        metadata = prepare_voice_fixture(self.path)
        self.assertEqual((metadata["bytes"], metadata["frames"], metadata["duration_ms"]), (480000, 240000, 10000))
        self.assertEqual(load_voice_fixture(self.path, metadata["sha256"]), maximum)
        self.assert_code("voice_fixture_pcm_invalid", lambda: describe_voice_pcm(maximum + b"\x01\x00"))

    def test_describe_accepts_only_exact_immutable_bytes(self):
        class BytesSubclass(bytes):
            pass
        for invalid in (True, 1, None, "pcm", bytearray(PCM), memoryview(PCM), BytesSubclass(PCM)):
            with self.subTest(kind=type(invalid).__name__):
                self.assert_code("voice_fixture_pcm_invalid", lambda: describe_voice_pcm(invalid))
        self.run.assert_not_called()

    def test_existing_file_and_symlink_preserved_without_running_binaries(self):
        self.path.write_bytes(b"existing")
        self.assert_code("voice_fixture_exists", lambda: prepare_voice_fixture(self.path))
        self.assertEqual(self.path.read_bytes(), b"existing")
        self.path.unlink()
        target = self.parent / "target"
        target.write_bytes(b"target original")
        self.path.symlink_to(target)
        self.assert_code("voice_fixture_exists", lambda: prepare_voice_fixture(self.path))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), b"target original")
        self.run.assert_not_called()

    def test_existing_parent_required_and_symlink_parent_rejected(self):
        self.assert_code("voice_fixture_path_invalid", lambda: prepare_voice_fixture(self.parent / "missing" / "x.pcm"))
        parent_alias = self.parent / "alias"
        parent_alias.symlink_to(self.parent, target_is_directory=True)
        self.assert_code("voice_fixture_path_invalid", lambda: prepare_voice_fixture(parent_alias / "x.pcm"))
        self.assertFalse((self.parent / "x.pcm").exists())
        self.run.assert_not_called()

    def test_path_created_during_conversion_is_not_overwritten(self):
        def convert(*args, **kwargs):
            if self.run.call_count == 1:
                return process(WAV)
            self.path.write_bytes(b"concurrent original")
            return process(PCM)
        self.run.side_effect = convert
        self.assert_code("voice_fixture_exists", lambda: prepare_voice_fixture(self.path))
        self.assertEqual(self.path.read_bytes(), b"concurrent original")

    def test_partial_write_failure_removes_only_owned_new_file(self):
        original_write = os.write
        writes = 0
        def fail_after_partial(fd, data):
            nonlocal writes
            writes += 1
            if writes == 1:
                return original_write(fd, data[:2])
            raise OSError("private write failure")
        with patch(MODULE + "os.write", side_effect=fail_after_partial):
            self.assert_code("voice_fixture_write_failed", lambda: prepare_voice_fixture(self.path))
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_cleanup_preserves_concurrent_replacement_inode(self):
        def replace_and_fail(fd, data):
            self.path.unlink()
            self.path.write_bytes(b"replacement original")
            raise OSError("private write failure")
        with patch(MODULE + "os.write", side_effect=replace_and_fail):
            self.assert_code("voice_fixture_write_failed", lambda: prepare_voice_fixture(self.path))
        self.assertEqual(self.path.read_bytes(), b"replacement original")

    def test_failed_fsync_and_zero_write_remove_owned_partial_file(self):
        with patch(MODULE + "os.fsync", side_effect=OSError("private sync")):
            self.assert_code("voice_fixture_write_failed", lambda: prepare_voice_fixture(self.path))
        self.assertFalse(self.path.exists())
        self.run.side_effect = [process(WAV), process(PCM)]
        with patch(MODULE + "os.write", return_value=0):
            self.assert_code("voice_fixture_write_failed", lambda: prepare_voice_fixture(self.path))
        self.assertFalse(self.path.exists())

    def test_load_validates_hash_shape_before_any_file_access(self):
        for invalid in (True, None, "", "a" * 63, "g" * 64, "A" * 64, "a" * 64 + "\n"):
            with self.subTest(value=invalid):
                self.assert_code("voice_fixture_hash_invalid", lambda: load_voice_fixture(self.path, invalid))
        self.run.assert_not_called()

    def test_load_exact_hash_mismatch_and_invalid_pcm_do_not_mutate_source(self):
        self.path.write_bytes(PCM)
        digest = hashlib.sha256(PCM).hexdigest()
        self.assertEqual(load_voice_fixture(self.path, digest), PCM)
        self.assert_code("voice_fixture_hash_mismatch", lambda: load_voice_fixture(self.path, "0" * 64))
        self.assertEqual(self.path.read_bytes(), PCM)
        for invalid in (b"", b"x", b"\0\0", b"x" * 480002):
            self.path.write_bytes(invalid)
            self.assert_code("voice_fixture_pcm_invalid", lambda: load_voice_fixture(self.path, hashlib.sha256(invalid).hexdigest()))
            self.assertEqual(self.path.read_bytes(), invalid)
        self.run.assert_not_called()

    def test_load_rejects_symlink_directory_fifo_without_blocking(self):
        target = self.parent / "target"
        target.write_bytes(PCM)
        self.path.symlink_to(target)
        self.assert_code("voice_fixture_read_failed", lambda: load_voice_fixture(self.path, hashlib.sha256(PCM).hexdigest()))
        self.assertEqual(target.read_bytes(), PCM)
        self.path.unlink()
        self.path.mkdir()
        self.assert_code("voice_fixture_pcm_invalid", lambda: load_voice_fixture(self.path, "0" * 64))
        self.path.rmdir()
        os.mkfifo(self.path)
        with patch(MODULE + "os.read") as read:
            self.assert_code("voice_fixture_pcm_invalid", lambda: load_voice_fixture(self.path, "0" * 64))
        read.assert_not_called()
        self.run.assert_not_called()

    def test_load_growth_is_bounded_to_maximum_plus_one(self):
        self.path.write_bytes(PCM)
        requests = []
        def growing_read(fd, size):
            requests.append(size)
            return b"x" * size
        with patch(MODULE + "os.read", side_effect=growing_read):
            self.assert_code("voice_fixture_pcm_invalid", lambda: load_voice_fixture(self.path, "0" * 64))
        self.assertEqual(sum(requests), MAX_PCM_BYTES + 1)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
