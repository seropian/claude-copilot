import http.server
import os
import signal
import socketserver
import sys
import threading
import time
import glob
import subprocess
import errno
import fcntl
import json
import urllib.request
import shlex
import stat
import shutil


def _lease_live(path):
    try:
        pid = int(os.path.basename(path))
        with open(path) as f:
            token = " ".join(f.read().split())
        if not token:
            return False
        got = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)], stderr=subprocess.DEVNULL).decode().split())
        return bool(got) and got == token
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _idle_grace():
    try:
        value = int(os.environ.get("COPILOT_SHIM_IDLE_GRACE", "30"))
    except ValueError:
        value = 30
    return max(0, value)


def _spare_idle(directory, binary):
    for path in glob.glob(os.path.join(directory, "leases", "*")):
        if _lease_live(path) and not _lease_live(os.path.join(directory, "spares", os.path.basename(path))):
            return False
    sessions = json.loads(subprocess.check_output(
        [binary, "agents", "--json"], stderr=subprocess.DEVNULL, timeout=10))
    if not isinstance(sessions, list):
        raise ValueError("Claude session registry did not return an array")
    if sessions:
        return False
    base = os.environ["ANTHROPIC_BASE_URL"]
    key = os.environ["ANTHROPIC_AUTH_TOKEN"]
    request = urllib.request.Request(base + "/_shim/status", headers={"x-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            status = json.load(response)
    except urllib.error.URLError as e:
        if isinstance(e.reason, OSError) and e.reason.errno == errno.ECONNREFUSED:
            return True
        raise
    inflight = status.get("inflight") if isinstance(status, dict) else None
    if type(inflight) is not int or inflight < 1:
        raise ValueError("shim status did not return a valid request count")
    # The status request itself is included in the count.
    return inflight == 1


def _spare_processes(pid):
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,lstart="]).decode().splitlines()
    processes = {}
    for row in rows:
        parts = row.split(None, 2)
        if len(parts) == 3:
            processes[int(parts[0])] = (int(parts[1]), parts[2].strip())
    owned = []
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        if current not in processes:
            continue
        owned.append((current, processes[current][1]))
        frontier.extend(p for p, (parent, _) in processes.items() if parent == current)
    return owned


def _signal_spare(processes, sig):
    live = []
    for pid, token in reversed(processes):
        try:
            got = subprocess.check_output(
                ["ps", "-o", "stat=,lstart=", "-p", str(pid)], stderr=subprocess.DEVNULL).decode().split(None, 1)
        except subprocess.CalledProcessError:
            continue
        if len(got) == 2 and not got[0].startswith("Z") and got[1].strip() == token:
            try:
                os.kill(pid, sig)
                live.append((pid, token))
            except ProcessLookupError:
                pass
    return live


def _process_rows():
    rows = subprocess.check_output(
        ["ps", "-ww", "-U", str(os.getuid()), "-axo", "pid=,ppid=,uid=,lstart=,command="]
    ).decode(errors="replace").splitlines()
    processes = {}
    for row in rows:
        parts = row.split(None, 8)
        if len(parts) == 9:
            try:
                pid, parent, uid = map(int, parts[:3])
            except ValueError:
                continue
            processes[pid] = {
                "parent": parent,
                "uid": uid,
                "start": " ".join(parts[3:8]),
                "command": parts[8],
            }
    return processes


def _launcher_arg(value, launcher):
    if not value.startswith("/") or any(c.isspace() for c in value + launcher):
        return False
    return os.path.normpath(value) == launcher or os.path.realpath(value) == os.path.realpath(launcher)


def _claude_arg(value):
    if not value.startswith("/") or any(c.isspace() for c in value):
        return False
    binary = shutil.which("claude")
    if binary and os.path.realpath(value) == os.path.realpath(binary):
        return True
    versions = os.path.expanduser("~/.local/share/claude/versions")
    return (
        os.path.dirname(value) == versions
        and os.path.dirname(os.path.realpath(value)) == os.path.realpath(versions)
        and os.path.basename(value) not in ("", ".", "..")
    )


def _legacy_socket(value, suffix):
    prefix = "/tmp/cc-daemon-%d/" % os.getuid()
    if not value.startswith(prefix) or os.path.normpath(value) != value:
        return False
    parts = value[len(prefix):].split("/")
    return (
        len(parts) == 3 and parts[1] == "spare"
        and parts[2].endswith(suffix)
        and all(part and all(c.isalnum() or c in "-_" for c in part)
                for part in (parts[0], parts[2][:-len(suffix)]))
    )


def _legacy_spare_args(args):
    return (
        len(args) in (3, 4) and args[2] == "--bg-spare"
        and (len(args) == 3 or _legacy_socket(args[3], ".claim.sock"))
    )


def _legacy_wrapper(command, launcher):
    # ps displays argv without quoting; shell syntax is ambiguous, not ownership proof.
    if any(c in command for c in ("\"", chr(39), "\\")):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    if os.path.basename(args[0]) in ("bash", "sh", "zsh") and len(args) > 1 and _launcher_arg(args[1], launcher):
        start = 1
    elif _launcher_arg(args[0], launcher):
        start = 0
    else:
        return False
    if len(args) >= start + 3 and _claude_arg(args[start + 1]):
        if args[start + 2] == "--bg-spare":
            return _legacy_spare_args(args[start:])
        if args[start + 2] == "--bg-pty-host":
            separator = start + 6
            if len(args) not in (separator + 4, separator + 5) or args[separator] != "--":
                return False
            if not _legacy_socket(args[start + 3], ".pty.sock"):
                return False
            if not all(value.isdecimal() and int(value) > 0 for value in args[start + 4:start + 6]):
                return False
            return (
                _launcher_arg(args[separator + 1], launcher)
                and _claude_arg(args[separator + 2])
                and _legacy_spare_args(args[separator + 1:])
                and (len(args) == separator + 4 or
                     args[separator + 4] == args[start + 3][:-len(".pty.sock")] + ".claim.sock")
            )
    return False


def _same_process(pid, uid, start):
    try:
        row = subprocess.check_output(
            ["ps", "-o", "uid=,stat=,lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        ).decode().split()
    except (OSError, subprocess.SubprocessError):
        return False
    return len(row) == 7 and row[0] == str(uid) and not row[1].startswith("Z") and " ".join(row[2:]) == start


def _state_file(directory):
    path = os.path.join(directory, "shim.state")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("shared shim state is not safely owned")
        fields = f.read().split()
    if len(fields) < 4:
        raise ValueError("shared shim state is incomplete")
    pid, port = int(fields[0]), int(fields[1])
    if pid < 1 or port < 1 or port > 65535 or not fields[2]:
        raise ValueError("shared shim state is invalid")
    return {
        "pid": pid,
        "port": port,
        "key": fields[2],
        "version": fields[3],
    }


def _shim_identity(process, state, directory):
    if process is None or process["uid"] != os.getuid():
        return False
    command = process["command"]
    executable = command.split(None, 1)[0] if command.split(None, 1) else ""
    if "python" not in os.path.basename(executable).lower() or " -c " not in command:
        return False
    try:
        prefix, port, start_file = command.rsplit(None, 2)
    except ValueError:
        return False
    if not prefix.endswith(" serve") or port != str(state["port"]):
        return False
    if os.path.dirname(start_file) != directory or not os.path.basename(start_file).startswith("start."):
        return False
    return "/_shim/status" in command and "CopilotRequestHandler" in command


def _shim_status(state):
    request = urllib.request.Request(
        "http://127.0.0.1:%d/_shim/status" % state["port"],
        headers={"x-api-key": state["key"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    inflight = payload.get("inflight") if isinstance(payload, dict) else None
    return inflight if type(inflight) is int and inflight >= 1 else None


def _lease_present(directory, owned=None):
    owned = owned or {}
    for name in ("leases", "spares"):
        path = os.path.join(directory, name)
        try:
            entries = os.listdir(path)
        except FileNotFoundError:
            continue
        for entry in entries:
            process = owned.get(int(entry)) if entry.isdecimal() else None
            if process is None:
                return True
            try:
                fd = os.open(os.path.join(path, entry), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd) as f:
                    info = os.fstat(f.fileno())
                    token = " ".join(f.read().split())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022 or token != process["start"]
                        or not _same_process(int(entry), process["uid"], token)):
                    return True
            except (OSError, ValueError):
                return True
    return False


def _cleanup_idle_now(directory, state, owned=None):
    try:
        sessions = json.loads(subprocess.check_output(
            ["claude", "agents", "--json"], stderr=subprocess.DEVNULL, timeout=10
        ))
    except (OSError, ValueError, subprocess.SubprocessError):
        log("shim: legacy cleanup deferred: Claude session registry unavailable")
        return False
    if not isinstance(sessions, list) or sessions:
        log("shim: legacy cleanup deferred: Claude session registry is not empty")
        return False
    if _lease_present(directory, owned):
        log("shim: legacy cleanup deferred: client leases appeared")
        return False
    if state is not None and _shim_status(state) != 1:
        log("shim: legacy cleanup deferred: shim is no longer idle")
        return False
    return True


def _terminate_legacy_tree(processes, root):
    tree = []
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        process = processes.get(parent)
        if process is None:
            continue
        if process["uid"] != os.getuid():
            return False
        tree.append((parent, process["start"], process["uid"]))
        children = [pid for pid, child in processes.items() if child["parent"] == parent]
        frontier.extend(children)
    for pid, start, uid in reversed(tree):
        if _same_process(pid, uid, start):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(_same_process(pid, uid, start) for pid, start, uid in tree):
        time.sleep(0.1)
    for pid, start, uid in reversed(tree):
        if _same_process(pid, uid, start):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
    return not any(_same_process(pid, uid, start) for pid, start, uid in tree)


def _take_shim_lock(directory):
    # Same mkdir lock as the launcher, but never waits: a busy lock means a launcher is starting.
    path = os.path.join(directory, "shim.lock")
    try:
        os.mkdir(path, 0o700)
    except OSError:
        return None
    try:
        with open(os.path.join(path, "pid"), "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        try:
            os.rmdir(path)
        except OSError:
            pass
        return None
    return path


def _drop_shim_lock(path):
    pid = os.path.join(path, "pid")
    try:
        with open(pid) as f:
            if f.read().strip() != str(os.getpid()):
                return
        os.unlink(pid)
        os.rmdir(path)
    except OSError:
        pass


def _stop_stale_shim(directory, state, start, owned):
    # Slow checks already ran without the lock. Under it, only recheck what a starting launcher could change,
    # signal, and drop the state file so no launcher reuses the dying shim. The wait happens after release.
    lock = _take_shim_lock(directory)
    if lock is None:
        log("shim: legacy cleanup deferred: a launcher holds the shim lock")
        return False
    try:
        try:
            current = _state_file(directory)
        except (OSError, ValueError):
            current = None
        if current != state:
            log("shim: legacy cleanup deferred: shared shim state changed")
            return False
        if _lease_present(directory, owned):
            log("shim: legacy cleanup deferred: client leases appeared")
            return False
        if not _same_process(state["pid"], os.getuid(), start):
            log("shim: legacy cleanup deferred: shared shim process changed")
            return False
        if _shim_status(state) != 1:
            log("shim: legacy cleanup deferred: shim is no longer idle")
            return False
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            log("shim: legacy cleanup deferred: cannot stop the owned shim")
            return False
        try:
            os.unlink(os.path.join(directory, "shim.state"))
        except FileNotFoundError:
            pass
        except OSError:
            log("shim: legacy cleanup: could not remove the stopped shim state")
    finally:
        _drop_shim_lock(lock)
    deadline = time.monotonic() + 3
    while _same_process(state["pid"], os.getuid(), start) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _same_process(state["pid"], os.getuid(), start):
        try:
            os.kill(state["pid"], signal.SIGKILL)
        except OSError:
            pass
        log("shim: legacy cleanup: old shim ignored SIGTERM; sent SIGKILL")
        deadline = time.monotonic() + 1
        while _same_process(state["pid"], os.getuid(), start) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _same_process(state["pid"], os.getuid(), start):
            log("shim: legacy cleanup deferred: owned shim did not stop")
            return False
    return True


def _cleanup_legacy_exclusive(directory, launcher, version):
    try:
        fd = os.open(os.path.join(directory, "legacy-cleanup.lock"),
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        log("shim: legacy cleanup deferred: cleanup lock unavailable")
        return 1
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("shim: legacy cleanup skipped: another cleanup is running")
            return 1
        log("shim: legacy cleanup started (pid %d)" % os.getpid())
        rc = _cleanup_legacy(directory, launcher, version)
        log("shim: legacy cleanup %s" % ("finished" if rc == 0 else "deferred; retries after a later session"))
        return rc
    finally:
        os.close(fd)


def _cleanup_legacy(directory, launcher, version):
    try:
        processes = _process_rows()
    except (OSError, subprocess.SubprocessError):
        log("shim: legacy cleanup deferred: process list unavailable")
        return 1
    candidates = [
        pid for pid, process in processes.items()
        if process["uid"] == os.getuid() and process["parent"] == 1
        and _legacy_wrapper(process["command"], launcher)
    ]
    owned = {}
    frontier = list(candidates)
    while frontier:
        pid = frontier.pop()
        if pid in owned:
            continue
        process = processes[pid]
        if process["uid"] != os.getuid() or not process["start"]:
            log("shim: legacy cleanup deferred: wrapper tree ownership is uncertain")
            return 1
        owned[pid] = process
        frontier.extend(child for child, row in processes.items() if row["parent"] == pid)
    uncertain = False
    for pid, process in processes.items():
        if process["uid"] != os.getuid() or launcher not in process["command"]:
            continue
        if "--bg-spare" not in process["command"] and "--bg-pty-host" not in process["command"]:
            continue
        if pid not in owned:
            uncertain = True
    if uncertain:
        log("shim: legacy cleanup deferred: wrapper ownership is uncertain")
        return 1

    try:
        state = _state_file(directory)
    except (OSError, ValueError):
        log("shim: legacy cleanup deferred: shared shim state is unknown")
        return 1
    shim_process = processes.get(state["pid"]) if state else None
    stale_shim = state is not None and state["version"] != version
    if not candidates and not stale_shim:
        return 0
    try:
        sessions = json.loads(subprocess.check_output(
            ["claude", "agents", "--json"], stderr=subprocess.DEVNULL, timeout=10
        ))
    except (OSError, ValueError, subprocess.SubprocessError):
        log("shim: legacy cleanup deferred: Claude session registry unavailable")
        return 1
    if not isinstance(sessions, list):
        log("shim: legacy cleanup deferred: Claude session registry is invalid")
        return 1
    if sessions or _lease_present(directory, owned):
        log("shim: legacy cleanup deferred: active sessions or client leases")
        return 1

    if state is not None and (candidates or stale_shim):
        if not _shim_identity(shim_process, state, directory):
            log("shim: legacy cleanup deferred: shared shim identity is uncertain")
            return 1
        inflight = _shim_status(state)
        if inflight is None:
            log("shim: legacy cleanup deferred: authenticated shim status is unavailable")
            return 1
        if inflight != 1:
            log("shim: legacy cleanup deferred: shim has in-flight requests")
            return 1
        if stale_shim:
            if not _cleanup_idle_now(directory, state, owned):
                return 1
            if not _stop_stale_shim(directory, state, shim_process["start"], owned):
                return 1
    if state is None and candidates:
        log("shim: legacy cleanup: no shared shim state; stopping only verified orphan wrappers")
    for pid in candidates:
        if not _cleanup_idle_now(directory, None if stale_shim else state, owned):
            return 1
        if not _terminate_legacy_tree(processes, pid):
            log("shim: legacy cleanup deferred: owned wrapper tree did not stop")
            return 1
    return 0


def _run_spare(directory, command):
    grace = _idle_grace()
    if not grace:
        return subprocess.call(command)
    child = subprocess.Popen(command, start_new_session=True)
    interrupted = threading.Event()
    received = []
    def stop(sig, frame):
        received.append(sig)
        interrupted.set()
    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    handlers = {sig: signal.signal(sig, stop) for sig in signals}
    empty_since = None
    last_error = None
    try:
        while child.poll() is None:
            if interrupted.is_set():
                break
            try:
                idle = _spare_idle(directory, command[0])
                last_error = None
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                message = str(e)
                if message != last_error:
                    log("shim: spare cleanup deferred: %s" % message)
                    last_error = message
                idle = False
            now = time.monotonic()
            if not idle:
                empty_since = None
            elif empty_since is None:
                empty_since = now
            elif now - empty_since >= grace:
                log("shim: no active sessions, stopping owned spare worker")
                break
            interrupted.wait(1)
        if child.poll() is None:
            processes = _spare_processes(child.pid)
            _signal_spare(processes, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            deadline = time.monotonic() + 5
            while _signal_spare(processes, 0) and time.monotonic() < deadline:
                time.sleep(0.1)
            # Also reap descendants that ignored TERM even if the host exited.
            _signal_spare(processes, signal.SIGKILL)
        result = child.wait()
        return 128 + received[0] if received else (128 - result if result < 0 else result)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _watch_leases(server, directory):
    if not directory:
        return
    grace = _idle_grace()
    if not grace:
        return
    empty_since = None
    while True:
        live = any(_lease_live(p) for p in glob.glob(os.path.join(directory, "*")))
        with server.lifecycle_lock:
            inflight = server.inflight
            draining = server.draining
        now = time.time()
        if live or inflight:
            empty_since = None
        elif empty_since is None:
            empty_since = now
        elif now - empty_since >= grace:
            state = os.environ.get("COPILOT_SHIM_STATE_FILE", "")
            try:
                if state and os.path.isfile(state):
                    with open(state) as f:
                        owner = f.read().split(None, 1)[0]
                    if owner == str(os.getpid()):
                        os.unlink(state)
            except OSError:
                pass
            log("shim: no live clients, exiting%s" % (" after retire" if draining else ""))
            os._exit(0)
        time.sleep(1)
from .server import CopilotRequestHandler
from .auth import login
from .config import KEY, log


class LocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.lifecycle_lock = threading.Lock()
        self.inflight = 0
        self.draining = False
        self.version = os.environ.get("COPILOT_SHIM_VERSION", "")
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        with self.lifecycle_lock:
            self.inflight += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.lifecycle_lock:
                self.inflight -= 1

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that can stall for tens of seconds on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        print("claude-copilot-shim %s" % (os.environ.get("COPILOT_SHIM_VERSION") or "unknown"))
        return 0
    if not argv:
        return 2
    if argv[0] == "login":
        return login()
    if argv[0] == "spare":
        if len(argv) < 3:
            return 2
        return _run_spare(argv[1], argv[2:])
    if argv[0] == "cleanup":
        if len(argv) != 4:
            return 2
        return _cleanup_legacy_exclusive(argv[1], os.path.abspath(argv[2]), argv[3])
    if argv[0] == "serve":
        if len(argv) < 3:
            return 2
        if not KEY:
            log("shim: COPILOT_SHIM_KEY is not set, refusing to serve without an API key")
            return 2
        srv = LocalServer(("127.0.0.1", int(argv[1])), CopilotRequestHandler)
        def bye(n, _f):
            log("shim: pid %d exiting on signal %d" % (os.getpid(), n))
            os._exit(0)
        for n in (signal.SIGTERM, signal.SIGINT):
            signal.signal(n, bye)
        lease_dir = os.environ.get("COPILOT_SHIM_LEASE_DIR", "")
        if lease_dir:
            threading.Thread(target=_watch_leases, args=(srv, lease_dir), daemon=True).start()
        with open(argv[2] + ".tmp", "w") as f:
            f.write("%d %d" % (os.getpid(), srv.server_address[1]))
        os.replace(argv[2] + ".tmp", argv[2])
        try:
            srv.serve_forever()
        except BaseException as e:
            log("shim: pid %d died: %r" % (os.getpid(), e))
            raise
    return 2
