"""Use the configured non-root identity, then exec the CLI for signal delivery."""
import os
import sys

if os.getuid() == 0:
    raise SystemExit("Container must run as an explicitly configured non-root UID/GID")
for key, actual in (("PUID", os.getuid()), ("PGID", os.getgid())):
    if os.environ.get(key) and int(os.environ[key]) != actual:
        raise SystemExit("Container identity does not match configured UID/GID")
os.umask(0o077)
os.execvp("teams-transcription", ["teams-transcription", *sys.argv[1:]])
