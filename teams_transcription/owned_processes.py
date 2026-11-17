"""Stop only subprocesses owned by this worker, with PID-reuse checks."""
import asyncio
import os
from pathlib import Path
import signal


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19], fields[0]
    except (OSError, IndexError):
        return None


class OwnedProcesses:
    def __init__(self):
        self.processes = []
        self.descendants = {}

    def add(self, process):
        if process is not None and type(getattr(process, 'pid', None)) is int and process not in self.processes:
            self.processes.append(process)

    def snapshot(self):
        pending = [p.pid for p in self.processes if p.returncode is None]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen or pid == os.getpid() or pid < 2:
                continue
            seen.add(pid)
            value = identity(pid)
            if value is None:
                continue
            self.descendants.setdefault(pid, value[0])
            try:
                pending.extend(int(x) for x in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
            except OSError:
                pass

    def live(self):
        return [pid for pid, stamp in self.descendants.items()
                if (value := identity(pid)) is not None and value[0] == stamp and value[1] != 'Z']

    def kill(self):
        self.snapshot()
        for pid in reversed(list(self.descendants)):
            value = identity(pid)
            if value and value[0] == self.descendants[pid] and value[1] != 'Z':
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def reap(self, deadline):
        tasks = [asyncio.create_task(p.wait()) for p in self.processes]
        while asyncio.get_running_loop().time() < deadline:
            if not self.live() and all(task.done() for task in tasks):
                return True
            await asyncio.sleep(min(.01, max(0, deadline - asyncio.get_running_loop().time())))
        for task in tasks:
            if not task.done():
                task.cancel()
        return not self.live() and all(p.returncode is not None for p in self.processes)
