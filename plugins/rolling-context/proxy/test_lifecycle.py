"""Smoke test for the rolling-context proxy lifecycle — stdlib only, fully hermetic.

Boots proxy/server.py in a subprocess with an isolated state dir (ROLLING_CONTEXT_STATE_DIR)
and a throwaway port (ROLLING_CONTEXT_PORT), so it never touches the user's real ~/.claude
state or the live :5588 gateway. Asserts the invariants the SessionStart hook relies on:

  - /health self-reports the version from plugin.json AND its own real listener PID
  - the self-written pidfile matches that PID (no wrapper-PID drift — the bug self-pidfile fixes)
  - bind-as-lock: a second instance on the same port exits 0 cleanly, the first stays sole owner

These are exactly the facts the hook's version-gate + squatter-takeover logic trusts; if any
break, the hook can falsely reuse, falsely "upgrade", or double-bind.

Run:  python -m unittest test_lifecycle      (from this proxy/ dir)
   or  python test_lifecycle.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
PLUGIN_JSON = HERE.parent / ".claude-plugin" / "plugin.json"

BOOT_TIMEOUT = 15.0   # generous: cold python start on Windows CI


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _plugin_version() -> str:
    return json.loads(PLUGIN_JSON.read_text(encoding="utf-8-sig"))["version"]


def _health(port: int, timeout: float = 1.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
        return json.load(r)


def _wait_health(port: int, deadline: float):
    while time.time() < deadline:
        try:
            return _health(port)
        except Exception:
            time.sleep(0.25)
    return None


class ProxyLifecycle(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        self.state = Path(tempfile.mkdtemp(prefix="rc-smoke-"))
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except Exception:
                    p.kill()
        shutil.rmtree(self.state, ignore_errors=True)

    def _spawn(self):
        env = dict(
            os.environ,
            ROLLING_CONTEXT_PORT=str(self.port),
            ROLLING_CONTEXT_STATE_DIR=str(self.state),
            # /health never calls upstream; pin a dummy so resolution can't hit the network.
            ANTHROPIC_BASE_URL="https://example.invalid",
        )
        p = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=str(HERE),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(p)
        return p

    def test_health_reports_version_and_own_pid(self):
        p = self._spawn()
        h = _wait_health(self.port, time.time() + BOOT_TIMEOUT)
        self.assertIsNotNone(h, "proxy did not answer /health within timeout")
        self.assertEqual(h.get("version"), _plugin_version(),
                         "/health version must match plugin.json")
        self.assertEqual(h.get("pid"), p.pid,
                         "/health pid must be the real listener pid (no wrapper drift)")
        pidfile = self.state / "rolling-context-proxy.pid"
        self.assertTrue(pidfile.exists(), "server must self-write the pidfile after bind")
        self.assertEqual(pidfile.read_text(encoding="utf-8").strip(), str(p.pid),
                         "pidfile must hold the real listener pid")

    def test_bind_as_lock_second_instance_exits_clean(self):
        a = self._spawn()
        self.assertIsNotNone(_wait_health(self.port, time.time() + BOOT_TIMEOUT),
                             "first instance did not come up")
        b = self._spawn()  # same port + state dir → must lose the bind race
        try:
            rc = b.wait(10)
        except subprocess.TimeoutExpired:
            rc = None
        self.assertEqual(rc, 0, "second instance must exit 0 (bind-as-lock), not linger or crash")
        self.assertIsNone(a.poll(), "first instance must stay up")
        self.assertEqual(_health(self.port).get("pid"), a.pid,
                         "first instance must remain the sole owner of the port")


if __name__ == "__main__":
    unittest.main(verbosity=2)
