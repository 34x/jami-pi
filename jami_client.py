"""Jami bridge stdio client — JSON-RPC 2.0 over stdin/stdout."""

import fcntl
import json
import os
import queue
import subprocess
import threading
import time


def jsonrpc_request(method, params=None, id=None):
    """Build a JSON-RPC 2.0 request dict."""
    req = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    if id is not None:
        req["id"] = id
    return req


def is_response(obj):
    """Check if a JSON-RPC object is a response (has 'result' or 'error')."""
    return "result" in obj or "error" in obj


def is_notification(obj):
    """Check if a JSON-RPC object is a notification (no 'id')."""
    return "id" not in obj


class JamiStdioClient:
    """JSON-RPC 2.0 client that talks to the jami-bridge binary over stdio.

    Spawns the SDK as a subprocess, sends requests on stdin,
    and reads responses/notifications from stdout.
    """

    def __init__(
        self, jami_binary="jami-bridge", bridge_args=None, verbose_bridge=False
    ):
        self.jami_binary = jami_binary
        self.bridge_args = bridge_args or []
        self.verbose_bridge = verbose_bridge
        self.proc = None
        self.reader_thread = None
        self.pending = {}
        self.pending_results = {}
        self.notifications = queue.Queue()
        self.lock = threading.Lock()  # protects pending dict + stdin writes
        self.next_id = 1
        self._buf = ""

    def start(self):
        """Launch the jami-bridge subprocess and start the reader thread."""
        self.proc = subprocess_start(
            self.jami_binary, self.bridge_args, self.verbose_bridge
        )
        # Start stderr reader thread only if verbose
        if self.verbose_bridge:
            self._stderr_thread = threading.Thread(
                target=self._stderr_reader, daemon=True
            )
            self._stderr_thread.start()
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()

        # Wait for onReady notification
        deadline = time.time() + 30
        while time.time() < deadline:
            evt = self.get_notification(timeout=1.0)
            if evt and evt.get("method") == "onReady":
                return
            if not self.is_alive():
                raise RuntimeError("jami-bridge process exited before ready")
        raise TimeoutError("Timed out waiting for jami-bridge onReady")

    def stop(self):
        """Shut down the SDK subprocess gracefully."""
        if self.proc:
            try:
                self.call("shutdown", timeout=2)
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    def is_alive(self):
        """Check if the SDK subprocess is still running."""
        return self.proc is not None and self.proc.poll() is None

    def call(self, method, params=None, id=None, timeout=10.0):
        """Send a JSON-RPC request and wait for the response.

        Returns the 'result' dict on success.
        Raises Exception on JSON-RPC error or timeout.
        """
        if id is None:
            id = self.next_id
            self.next_id += 1

        req = jsonrpc_request(method, params, id)
        req_json = json.dumps(req)

        event = threading.Event()
        with self.lock:
            self.pending[id] = event
            self.pending_results[id] = None

        # Write to stdin (lock protects against concurrent writes from pi threads)
        with self.lock:
            if self.proc and self.proc.stdin:
                self.proc.stdin.write((req_json + "\n").encode("utf-8"))
                self.proc.stdin.flush()

        # Wait for response
        if not event.wait(timeout=timeout):
            with self.lock:
                self.pending.pop(id, None)
                self.pending_results.pop(id, None)
            raise TimeoutError(f"Timeout waiting for response to {method}")

        with self.lock:
            result = self.pending_results.pop(id, None)
            self.pending.pop(id, None)

        if result is None:
            raise RuntimeError(f"No result for {method}")

        if "error" in result:
            err = result["error"]
            raise Exception(f"SDK error: {err.get('message', err)}")

        return result.get("result", {})

    def get_notification(self, timeout=1.0):
        """Get the next notification from the SDK (blocking with timeout)."""
        try:
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def _stderr_reader(self):
        """Reader thread for bridge stderr: forwards logs to Python stderr."""
        import sys

        try:
            for line in self.proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[bridge] {text}", file=sys.stderr)
        except Exception:
            pass

    def _reader(self):
        """Reader thread: read JSON-RPC from SDK stdout and dispatch."""
        fd = self.proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        while True:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                self._buf += data.decode("utf-8", errors="replace")
            except BlockingIOError:
                time.sleep(0.1)
                continue
            except OSError:
                break

            # Process complete lines
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Skip non-JSON lines (e.g. pjlib init message)
                    continue
                self._dispatch(obj)

    def _dispatch(self, obj):
        """Route a JSON-RPC object to the right handler."""
        if is_response(obj):
            rid = obj.get("id")
            with self.lock:
                if rid in self.pending:
                    self.pending_results[rid] = obj
                    self.pending[rid].set()
        elif is_notification(obj):
            self.notifications.put(obj)


def subprocess_start(jami_binary, bridge_args=None, verbose_bridge=False):
    """Start the jami-bridge as a subprocess with stdin/stdout pipes.

    bridge_args: optional list of extra CLI args to pass to the bridge
                     (e.g. ["--auto-accept-from", "jami://abc123"])
    verbose_bridge: if True, pipe stderr so bridge logs are visible;
                    if False, discard stderr (quiet default)
    """
    cmd = [jami_binary, "--stdio"]
    if bridge_args:
        cmd.extend(bridge_args)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if verbose_bridge else subprocess.DEVNULL,
    )
