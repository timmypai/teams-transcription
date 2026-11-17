"""Explicit, bounded synthetic speech fixture; preparation never plays audio.

Call preparation only in an environment authorized to execute these binaries.
Cloud tests replace subprocess.run; importing and describing PCM execute nothing.
subprocess.run's timeout kills and drains its child before raising TimeoutExpired.
"""
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import time

VOICE_TEXT = "Please summarize the supplied meeting notes and return the requested JSON and completion marker."
UTTERANCE = VOICE_TEXT
MAX_PCM_BYTES = 480000
DEADLINE_SECONDS = 15
_MAX_WAV_BYTES = 1048576
_ESPEAK = "/usr/bin/espeak-ng"
_FFMPEG = "/usr/bin/ffmpeg"


def describe_voice_pcm(data):
    """Validate immutable 24 kHz mono PCM16LE and return only bounded metadata."""
    if type(data) is not bytes or not data or len(data) > MAX_PCM_BYTES or len(data) % 2:
        raise ValueError("voice_fixture_pcm_invalid")
    nonzero = sum(sample != 0 for (sample,) in struct.iter_unpack("<h", data))
    if not nonzero:
        raise ValueError("voice_fixture_pcm_invalid")
    frames = len(data) // 2
    return {"sample_rate": 24000, "channels": 1, "format": "pcm16le",
            "bytes": len(data), "frames": frames, "duration_ms": frames / 24,
            "nonzero_frames": nonzero, "sha256": hashlib.sha256(data).hexdigest()}


def _parent(path):
    try:
        if isinstance(path, bool):
            raise TypeError
        target = Path(path)
        if target.name in {"", ".", ".."}:
            raise ValueError
        parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        return parent_fd, target.name
    except (OSError, ValueError, TypeError):
        raise ValueError("voice_fixture_path_invalid") from None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("voice_fixture_timeout")
    return remaining


def _command(args, deadline, failure_code, *, input_data=None):
    timeout = _remaining(deadline)
    try:
        result = subprocess.run(args, input=input_data, capture_output=True,
                                timeout=timeout, check=False)
        if type(result.returncode) is not int or result.returncode:
            raise ValueError(failure_code)
        return result.stdout
    except subprocess.TimeoutExpired:
        raise ValueError("voice_fixture_timeout") from None
    except Exception:
        raise ValueError(failure_code) from None


def _remove_owned(parent_fd, name, created):
    """Remove only the newly created inode, preserving any replacement path."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def prepare_voice_fixture(path):
    """Generate fixed speech in memory, exclusively create PCM file, return metadata.

    The parent must already exist. Existing entries and symlinks are preserved.
    No WAV, stderr, transcript, path, or audio bytes are returned or logged.
    """
    try:
        return _prepare_voice_fixture(path)
    except Exception as error:
        code = str(error) if type(error) is ValueError else ""
        allowed = {"voice_fixture_timeout", "voice_fixture_path_invalid", "voice_fixture_exists",
                   "voice_fixture_synthesis_failed", "voice_fixture_conversion_failed",
                   "voice_fixture_wav_invalid", "voice_fixture_pcm_invalid", "voice_fixture_write_failed"}
        raise ValueError(code if code in allowed else "voice_fixture_prepare_failed") from None


def _prepare_voice_fixture(path):
    parent_fd, name = _parent(path)
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            raise ValueError("voice_fixture_path_invalid") from None
        else:
            raise ValueError("voice_fixture_exists")
        deadline = time.monotonic() + DEADLINE_SECONDS
        wav = _command([_ESPEAK, "--stdout", "-v", "en-us", "-s", "160", UTTERANCE],
                       deadline, "voice_fixture_synthesis_failed")
        if type(wav) is not bytes or not wav or len(wav) > _MAX_WAV_BYTES:
            raise ValueError("voice_fixture_wav_invalid")
        pcm = _command([_FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "wav", "-i", "pipe:0", "-ac", "1", "-ar", "24000",
                        "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                       deadline, "voice_fixture_conversion_failed", input_data=wav)
        metadata = describe_voice_pcm(pcm)
        _remaining(deadline)
        fd, created = None, None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent_fd)
            created = os.fstat(fd)
            if not stat.S_ISREG(created.st_mode):
                raise OSError
            os.fchmod(fd, 0o600)
            pending = memoryview(pcm)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError
                pending = pending[written:]
            os.fsync(fd)
        except FileExistsError:
            raise ValueError("voice_fixture_exists") from None
        except Exception:
            if created is not None:
                _remove_owned(parent_fd, name, created)
            raise ValueError("voice_fixture_write_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
        return metadata
    finally:
        os.close(parent_fd)


def load_voice_fixture(path, expected_sha256):
    """Read a bounded regular non-symlink file and verify its immutable PCM hash."""
    if type(expected_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("voice_fixture_hash_invalid")
    parent_fd, name = _parent(path)
    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=parent_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PCM_BYTES:
                raise ValueError("voice_fixture_pcm_invalid")
            chunks, remaining = [], MAX_PCM_BYTES + 1
            while remaining:
                part = os.read(fd, min(65536, remaining))
                if not part:
                    break
                chunks.append(part)
                remaining -= len(part)
            data = b"".join(chunks)
        except OSError:
            raise ValueError("voice_fixture_read_failed") from None
        metadata = describe_voice_pcm(data)
        if not hmac.compare_digest(metadata["sha256"], expected_sha256):
            raise ValueError("voice_fixture_hash_mismatch")
        return data
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)
