#!/usr/bin/env python3
"""Mobile Build Monitor dashboard — local server.

Serves the dashboard page and brokers devicectl commands:
  GET    /                 dashboard page
  GET    /api/builds       active build manifests (newest first)
  GET    /api/removed-builds  soft-removed build manifests (newest first)
  GET    /api/devices      paired physical devices via devicectl
  GET    /api/settings     local dashboard settings
  POST   /api/install      {"buildId": ..., "deviceId": ...}
                           streams install output as plain text
  POST   /api/builds/<id>/record-launch  records a successful install-and-run
  DELETE /api/builds/<id>  soft-remove a manifest (leaves the artifact on disk)
  DELETE /api/removed-builds  permanently delete all removed manifests
  DELETE /api/removed-builds/<id>  permanently delete a removed manifest
  POST   /api/builds/restore  restore a recently removed manifest

Binds 127.0.0.1 only. Zero dependencies (python3 stdlib).
"""

import datetime
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("MOBILE_BUILD_MONITOR_DIR", str(Path.home() / ".mobile-build-monitor")))
BUILDS_DIR = ROOT / "builds"
REMOVED_BUILDS_DIR = ROOT / ".removed-build-manifests"
SETTINGS_PATH = ROOT / "settings.json"
BUILD_WRITE_LOCK = threading.Lock()
SETTINGS_WRITE_LOCK = threading.Lock()
REMOVED_BUILDS_WRITE_LOCK = threading.Lock()
INDEX = Path(__file__).parent / "index.html"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8484

ADB = shutil.which("adb") or str(Path.home() / "Library/Android/sdk/platform-tools/adb")
SSH = shutil.which("ssh")
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")
MOCK_DATA_LOCK = threading.Lock()


def iso_ago(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mock_build(build_id, *, os_name, worktree, branch, build_number, minutes, runs=0, last_run_minutes=None, removed_id=None, removed_minutes=None):
    app_name = "Example Mobile" if os_name == "ios" else "Example Mobile Android"
    extension = "app" if os_name == "ios" else "apk"
    manifest = {
        "schemaVersion": 1,
        "id": build_id,
        "os": os_name,
        "builtAt": iso_ago(minutes + 3),
        "publishedAt": iso_ago(minutes),
        "scheme": "Example Staging" if os_name == "ios" else "stagingDebug",
        "configuration": "Staging_Debug" if os_name == "ios" else "stagingDebug",
        "platform": "iphoneos" if os_name == "ios" else "android",
        "app": {
            "name": app_name, "bundleId": "com.example.mobile.staging",
            "version": "1.0.0", "buildNumber": str(build_number),
            "path": f"/mock-builds/{build_id}/Example Mobile.{extension}",
        },
        "source": {
            "worktree": worktree, "branch": branch,
            "commit": f"a{build_number:06x}"[-7:],
            "pr": f"https://github.com/example/mobile-app/pull/{build_number}",
        },
        "notes": "Mock build data — safe to install, run, remove, or restore.",
        "runCount": runs,
    }
    if last_run_minutes is not None:
        manifest["lastRunAt"] = iso_ago(last_run_minutes)
    if removed_id is not None:
        manifest["removedId"] = removed_id
        manifest["removedAt"] = iso_ago(removed_minutes)
    return manifest


def make_mock_data():
    checkout = "/Users/demo/worktrees/feature-checkout"
    notifications = "/Users/demo/worktrees/feature-notifications"
    active = [
        mock_build("mock-ios-checkout-43", os_name="ios", worktree=checkout, branch="feature/checkout", build_number=843, minutes=8, runs=4, last_run_minutes=2),
        mock_build("mock-ios-checkout-42", os_name="ios", worktree=checkout, branch="feature/checkout", build_number=842, minutes=38, runs=1, last_run_minutes=26),
        mock_build("mock-ios-notifications-57", os_name="ios", worktree=notifications, branch="feature/notifications", build_number=857, minutes=62),
        mock_build("mock-android-checkout-43", os_name="android", worktree=checkout, branch="feature/checkout", build_number=943, minutes=13, runs=2, last_run_minutes=9),
        mock_build("mock-android-notifications-57", os_name="android", worktree=notifications, branch="feature/notifications", build_number=957, minutes=73),
    ]
    removed = [
        mock_build("mock-ios-checkout-41", os_name="ios", worktree=checkout, branch="feature/checkout", build_number=841, minutes=95, removed_id="1" * 32, removed_minutes=31),
        mock_build("mock-ios-notifications-56", os_name="ios", worktree=notifications, branch="feature/notifications", build_number=856, minutes=160, removed_id="2" * 32, removed_minutes=84),
        mock_build("mock-android-checkout-42", os_name="android", worktree=checkout, branch="feature/checkout", build_number=942, minutes=110, removed_id="3" * 32, removed_minutes=47),
    ]
    return active, removed


MOCK_BUILDS, MOCK_REMOVED_BUILDS = make_mock_data()


def mock_data_enabled():
    return load_settings().get("mockDataEnabled") is True


def save_mock_data_enabled(enabled):
    if not isinstance(enabled, bool):
        raise ValueError("mock data enabled must be a boolean")
    with SETTINGS_WRITE_LOCK:
        settings = load_settings()
        settings["mockDataEnabled"] = enabled
        ROOT.mkdir(parents=True, exist_ok=True)
        temporary_settings = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=ROOT,
                prefix=".settings.", suffix=".tmp", delete=False,
            ) as f:
                temporary_settings = Path(f.name)
                json.dump(settings, f, indent=2)
                f.write("\n")
            temporary_settings.replace(SETTINGS_PATH)
        except OSError as e:
            raise RuntimeError(f"could not save settings: {e}") from e
        finally:
            if temporary_settings is not None:
                try:
                    temporary_settings.unlink()
                except FileNotFoundError:
                    pass


def load_mock_builds(removed=False):
    with MOCK_DATA_LOCK:
        builds = MOCK_REMOVED_BUILDS if removed else MOCK_BUILDS
        return copy.deepcopy(builds)


def mock_build_for(build_id):
    with MOCK_DATA_LOCK:
        for build in MOCK_BUILDS:
            if build["id"] == build_id:
                return build
    return None


def mock_removed_build_for(removed_id):
    with MOCK_DATA_LOCK:
        for build in MOCK_REMOVED_BUILDS:
            if build["removedId"] == removed_id:
                return build
    return None


def load_settings():
    """Load optional local settings without making the dashboard unavailable.

    A malformed settings file is surfaced through the settings API rather than
    preventing existing local builds from being listed or installed.
    """
    try:
        if not SETTINGS_PATH.is_file():
            return {}
        settings = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not read settings: {e}") from e
    if not isinstance(settings, dict):
        raise RuntimeError("could not read settings: root must be an object")
    return settings


def configured_remote_builder():
    settings = load_settings()
    builder = settings.get("remoteBuilder")
    if builder is None:
        return None
    if not isinstance(builder, dict):
        raise RuntimeError("could not read settings: remoteBuilder must be an object")
    host = builder.get("sshHost")
    directory = builder.get("directory")
    if not isinstance(host, str) or not SSH_HOST_RE.fullmatch(host):
        raise RuntimeError("could not read settings: remoteBuilder.sshHost is invalid")
    if (not isinstance(directory, str) or not directory.startswith("/")
            or len(directory) > 1024 or "\x00" in directory
            or any(c in directory for c in "\r\n")):
        raise RuntimeError("could not read settings: remoteBuilder.directory is invalid")
    return {"sshHost": host, "directory": directory}


def validate_remote_builder(request):
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    host = request.get("sshHost")
    directory = request.get("directory")
    if not isinstance(host, str) or not SSH_HOST_RE.fullmatch(host):
        raise ValueError("SSH host must be an SSH config alias (letters, numbers, ., _, or -)")
    if (not isinstance(directory, str) or not directory.startswith("/")
            or len(directory) > 1024 or "\x00" in directory
            or any(c in directory for c in "\r\n")):
        raise ValueError("builder directory must be an absolute path without line breaks")
    return {"sshHost": host, "directory": directory}


def save_remote_builder(builder):
    """Atomically persist the builder descriptor. SSH credentials stay in ~/.ssh."""
    with SETTINGS_WRITE_LOCK:
        settings = load_settings()
        settings["remoteBuilder"] = builder
        ROOT.mkdir(parents=True, exist_ok=True)
        temporary_settings = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=ROOT,
                prefix=".settings.", suffix=".tmp", delete=False,
            ) as f:
                temporary_settings = Path(f.name)
                json.dump(settings, f, indent=2)
                f.write("\n")
            temporary_settings.replace(SETTINGS_PATH)
        except OSError as e:
            raise RuntimeError(f"could not save settings: {e}") from e
        finally:
            if temporary_settings is not None:
                try:
                    temporary_settings.unlink()
                except FileNotFoundError:
                    pass


def check_remote_builder(builder):
    """Check SSH authentication and the remote monitor directory without writing.

    The host is constrained to an SSH config alias and the remote path is shell
    quoted before it is passed to SSH. This keeps a browser-provided setting
    from changing the remote command being executed.
    """
    if not SSH:
        return {"reachable": False, "directoryReady": False,
                "message": "ssh was not found on this Mac"}
    remote_command = (
        f"if test -d {shlex.quote(builder['directory'])} "
        f"&& test -w {shlex.quote(builder['directory'])}; then "
        "printf remote-builder-directory-ready; "
        "else printf remote-builder-directory-not-ready; fi"
    )
    command = [
        SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1", builder["sshHost"], remote_command,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"reachable": False, "directoryReady": False,
                "message": "SSH connection timed out after 15 seconds"}
    except OSError as e:
        return {"reachable": False, "directoryReady": False,
                "message": f"could not start SSH: {e}"}

    output = result.stdout.strip()
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["SSH failed"]
        return {"reachable": False, "directoryReady": False,
                "message": detail[0][:500]}
    if output == "remote-builder-directory-ready":
        return {"reachable": True, "directoryReady": True,
                "message": "SSH connected; builder directory is writable"}
    return {"reachable": True, "directoryReady": False,
            "message": "SSH connected, but the builder directory does not exist or is not writable"}


def load_builds():
    if mock_data_enabled():
        builds = load_mock_builds()
        for build in builds:
            build["appExists"] = True
        builds.sort(key=lambda b: b.get("publishedAt") or b.get("builtAt", ""), reverse=True)
        return builds
    builds = []
    for f in sorted(BUILDS_DIR.glob("*.json")):
        try:
            m = json.loads(f.read_text())
        except Exception as e:
            builds.append({"id": f.stem, "invalid": f"unparseable manifest: {e}"})
            continue
        m["os"] = m.get("os", "ios")
        artifact = Path(m.get("app", {}).get("path", "/nonexistent"))
        m["appExists"] = artifact.is_dir() if m["os"] == "ios" else artifact.is_file()
        builds.append(m)
    builds.sort(key=lambda b: b.get("publishedAt") or b.get("builtAt", ""), reverse=True)
    return builds


def load_removed_builds():
    """Return soft-removed manifests, newest removal first."""
    if mock_data_enabled():
        builds = load_mock_builds(removed=True)
        for build in builds:
            build["appExists"] = True
        builds.sort(key=lambda b: b.get("removedAt", ""), reverse=True)
        return builds
    if not REMOVED_BUILDS_DIR.is_dir():
        return []
    builds = []
    for f in REMOVED_BUILDS_DIR.glob("*.json"):
        removed_id = f.name[:32]
        if len(removed_id) != 32 or f.name[32:33] != "-":
            continue
        try:
            m = json.loads(f.read_text())
            removed_at = datetime.datetime.fromtimestamp(
                f.stat().st_mtime, datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(m, dict):
            continue
        m["os"] = m.get("os", "ios")
        artifact = Path(m.get("app", {}).get("path", "/nonexistent"))
        m["appExists"] = artifact.is_dir() if m["os"] == "ios" else artifact.is_file()
        m["removedId"] = removed_id
        m["removedAt"] = removed_at
        builds.append(m)
    builds.sort(key=lambda b: b["removedAt"], reverse=True)
    return builds


def removed_manifest_for(removed_id):
    """Find the saved manifest for a server-generated removal identifier."""
    if (not isinstance(removed_id, str) or len(removed_id) != 32
            or any(c not in "0123456789abcdef" for c in removed_id)):
        return None
    saved_manifests = list(REMOVED_BUILDS_DIR.glob(f"{removed_id}-*.json"))
    if len(saved_manifests) != 1 or not saved_manifests[0].is_file():
        return None
    return saved_manifests[0]


def valid_build_id(build_id):
    return bool(build_id) and "/" not in build_id and ".." not in build_id


def probe_device(identifier):
    """Live connection state for one device via `device info details`.

    `list devices` reports a stale tunnelState for wired devices (it tracks the
    wireless tunnel), so each device is probed directly. Unreachable devices
    answer 'unavailable' in ~1s; returns None on error/timeout.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            res = subprocess.run(
                ["xcrun", "devicectl", "device", "info", "details",
                 "--device", identifier, "--json-output", tmp.name],
                capture_output=True, text=True, timeout=20,
            )
            if res.returncode != 0:
                return None
            return json.load(open(tmp.name)).get("result", {}).get("connectionProperties", {})
    except Exception:
        return None


def ios_devices():
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        res = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", tmp.name],
            capture_output=True, text=True, timeout=60,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "devicectl failed")
        data = json.load(open(tmp.name))
    physical = [
        d for d in data.get("result", {}).get("devices", [])
        if d.get("hardwareProperties", {}).get("reality") != "simulated"
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(lambda d: probe_device(d.get("identifier")), physical))
    devices = []
    for d, live in zip(physical, probes):
        hw = d.get("hardwareProperties", {})
        conn = live or d.get("connectionProperties", {})
        props = d.get("deviceProperties", {})
        devices.append({
            "os": "ios",
            "identifier": d.get("identifier"),
            "udid": hw.get("udid"),
            "name": props.get("name") or hw.get("marketingName") or "device",
            "marketingName": hw.get("marketingName"),
            "osVersion": props.get("osVersionNumber"),
            "pairingState": conn.get("pairingState"),
            "state": conn.get("tunnelState"),  # connected / disconnected / unavailable
            "transport": conn.get("transportType"),  # wired / localNetwork
            "lastConnection": conn.get("lastConnectionDate"),
        })
    return devices


ADB_STATES = {"device": "connected", "offline": "disconnected"}


def adb(serial, *args, timeout=15):
    res = subprocess.run([ADB, "-s", serial, *args],
                         capture_output=True, text=True, timeout=timeout)
    return res.stdout.strip() if res.returncode == 0 else None


def android_devices():
    if not Path(ADB).is_file():
        return []
    res = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return []
    entries = []
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] == "*":
            continue
        serial, state = parts[0], parts[1]
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        entries.append((serial, state, props))

    def os_version(entry):
        serial, state, _ = entry
        if state != "device":
            return None
        return adb(serial, "shell", "getprop", "ro.build.version.release")

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(os_version, entries))
    devices = []
    for (serial, state, props), version in zip(entries, versions):
        model = props.get("model", "").replace("_", " ") or serial
        devices.append({
            "os": "android",
            "identifier": serial,
            "udid": serial,
            "name": model,
            "marketingName": model,
            "osVersion": version,
            "pairingState": state,  # device / offline / unauthorized
            "state": ADB_STATES.get(state, "unavailable"),
            "transport": "localNetwork" if ":" in serial else "wired",
            "lastConnection": None,
        })
    return devices


def load_devices():
    if mock_data_enabled():
        return [
            {"os": "ios", "identifier": "mock-ios-17-pro", "name": "Taylor’s iPhone", "marketingName": "iPhone 17 Pro", "osVersion": "26.0", "state": "connected", "transport": "localNetwork"},
            {"os": "ios", "identifier": "mock-ios-16", "name": "Test iPhone", "marketingName": "iPhone 16", "osVersion": "26.0", "state": "connected", "transport": "wired"},
            {"os": "android", "identifier": "mock-android-pixel", "name": "Pixel Test Device", "marketingName": "Pixel 10", "osVersion": "16", "state": "connected", "transport": "wired"},
        ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ios_f = pool.submit(ios_devices)
        android_f = pool.submit(android_devices)
        errors = []
        try:
            devices = ios_f.result()
        except Exception as e:
            devices, errors = [], [f"ios: {e}"]
        try:
            devices += android_f.result()
        except Exception as e:
            errors.append(f"android: {e}")
    if errors and not devices:
        raise RuntimeError("; ".join(errors))
    return devices


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/builds":
            self.send_json(load_builds())
        elif self.path == "/api/removed-builds":
            self.send_json(load_removed_builds())
        elif self.path == "/api/devices":
            try:
                self.send_json(load_devices())
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
        elif self.path == "/api/settings":
            try:
                self.send_json({
                    "buildsDirectory": str(BUILDS_DIR),
                    "remoteBuilder": configured_remote_builder(),
                    "mockDataEnabled": mock_data_enabled(),
                })
            except RuntimeError as e:
                self.send_json({"error": str(e)}, status=500)
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
        if self.path == "/api/removed-builds":
            return self.handle_clear_removed_builds()
        removed_prefix = "/api/removed-builds/"
        if self.path.startswith(removed_prefix):
            return self.handle_hard_delete(self.path[len(removed_prefix):])
        prefix = "/api/builds/"
        if not self.path.startswith(prefix):
            return self.send_json({"error": "not found"}, status=404)
        build_id = self.path[len(prefix):]
        # id is a filename stem; refuse anything path-like
        if not valid_build_id(build_id):
            return self.send_json({"error": "bad id"}, status=400)
        if mock_data_enabled():
            with MOCK_DATA_LOCK:
                for index, build in enumerate(MOCK_BUILDS):
                    if build["id"] == build_id:
                        removed = MOCK_BUILDS.pop(index)
                        removed["removedId"] = uuid.uuid4().hex
                        removed["removedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        MOCK_REMOVED_BUILDS.insert(0, removed)
                        return self.send_json({"ok": True, "removedId": removed["removedId"], "buildId": build_id})
            return self.send_json({"error": "unknown build"}, status=404)
        manifest = BUILDS_DIR / f"{build_id}.json"
        if not manifest.is_file():
            return self.send_json({"error": "unknown build"}, status=404)
        # Keep the manifest outside the active list. The restore endpoint moves
        # this exact file back without trusting the browser to reconstruct it.
        with REMOVED_BUILDS_WRITE_LOCK:
            REMOVED_BUILDS_DIR.mkdir(parents=True, exist_ok=True)
            removed_id = uuid.uuid4().hex
            removed_manifest = REMOVED_BUILDS_DIR / f"{removed_id}-{build_id}.json"
            try:
                manifest.replace(removed_manifest)
            except OSError as e:
                return self.send_json({"error": f"could not remove build: {e}"}, status=500)
        self.send_json({"ok": True, "removedId": removed_id, "buildId": build_id})

    def handle_hard_delete(self, removed_id):
        if mock_data_enabled():
            with MOCK_DATA_LOCK:
                for index, build in enumerate(MOCK_REMOVED_BUILDS):
                    if build["removedId"] == removed_id:
                        MOCK_REMOVED_BUILDS.pop(index)
                        return self.send_json({"ok": True})
            return self.send_json({"error": "unknown removed build"}, status=404)
        with REMOVED_BUILDS_WRITE_LOCK:
            removed_manifest = removed_manifest_for(removed_id)
            if removed_manifest is None:
                return self.send_json({"error": "unknown removed build"}, status=404)
            try:
                removed_manifest.unlink()
            except OSError as e:
                return self.send_json({"error": f"could not permanently delete build: {e}"}, status=500)
        self.send_json({"ok": True})

    def handle_clear_removed_builds(self):
        """Permanently delete every valid removed manifest, never its artifact."""
        if mock_data_enabled():
            with MOCK_DATA_LOCK:
                deleted_count = len(MOCK_REMOVED_BUILDS)
                MOCK_REMOVED_BUILDS.clear()
            return self.send_json({"ok": True, "deletedCount": deleted_count})
        with REMOVED_BUILDS_WRITE_LOCK:
            removed_manifests = []
            if REMOVED_BUILDS_DIR.is_dir():
                for candidate in REMOVED_BUILDS_DIR.glob("*.json"):
                    if removed_manifest_for(candidate.name[:32]) == candidate:
                        removed_manifests.append(candidate)
            deleted_count = 0
            failures = []
            for manifest in removed_manifests:
                try:
                    manifest.unlink()
                    deleted_count += 1
                except OSError as e:
                    failures.append(f"{manifest.name}: {e}")
        if failures:
            return self.send_json({
                "error": f"deleted {deleted_count} removed manifests, but {len(failures)} could not be deleted: {failures[0]}",
                "deletedCount": deleted_count,
            }, status=500)
        self.send_json({"ok": True, "deletedCount": deleted_count})

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def resolve_build(self, build_id):
        if mock_data_enabled():
            build = mock_build_for(build_id)
            return copy.deepcopy(build) if build else None
        manifest_path = BUILDS_DIR / f"{Path(build_id).name}.json"
        if not manifest_path.is_file():
            return None
        return json.loads(manifest_path.read_text())

    def start_stream_response(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def emit(self, line):
        data = (line.rstrip("\n") + "\n").encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def end_stream(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def stream_command(self, cmd, hard_kill=False):
        """Run cmd, streaming output lines to the client. Ends the process if
        the client disconnects. Returns the exit code, or None on disconnect.

        hard_kill uses SIGKILL: devicectl forwards catchable signals to a
        console-attached app, so SIGTERM on detach would terminate the app on
        the device — SIGKILL can't be caught, so only devicectl dies."""
        self.emit(f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        try:
            for line in proc.stdout:
                self.emit(line)
            proc.wait()
            return proc.returncode
        except (BrokenPipeError, ConnectionResetError):
            if hard_kill:
                proc.kill()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return None

    def do_POST(self):
        if self.path == "/api/settings/mock-data":
            return self.handle_set_mock_data()
        if self.path == "/api/remote-builder":
            return self.handle_save_remote_builder()
        if self.path == "/api/remote-builder/check":
            return self.handle_check_remote_builder()
        if self.path == "/api/builds/restore":
            return self.handle_restore_build()
        prefix, suffix = "/api/builds/", "/record-launch"
        if self.path.startswith(prefix) and self.path.endswith(suffix):
            return self.handle_record_launch(self.path[len(prefix):-len(suffix)])
        if self.path == "/api/install":
            return self.handle_install()
        if self.path == "/api/launch":
            return self.handle_launch()
        self.send_json({"error": "not found"}, status=404)

    def handle_set_mock_data(self):
        try:
            request = self.read_body()
            enabled = request.get("enabled") if isinstance(request, dict) else None
            save_mock_data_enabled(enabled)
        except ValueError as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        except RuntimeError as e:
            return self.send_json({"error": str(e)}, status=500)
        self.send_json({"ok": True, "mockDataEnabled": enabled})

    def handle_save_remote_builder(self):
        try:
            builder = validate_remote_builder(self.read_body())
            save_remote_builder(builder)
        except ValueError as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        except RuntimeError as e:
            return self.send_json({"error": str(e)}, status=500)
        self.send_json({"ok": True, "remoteBuilder": builder})

    def handle_check_remote_builder(self):
        try:
            builder = configured_remote_builder()
        except RuntimeError as e:
            return self.send_json({"error": str(e)}, status=500)
        if builder is None:
            return self.send_json({"error": "remote builder is not configured"}, status=404)
        self.send_json(check_remote_builder(builder))

    def handle_restore_build(self):
        try:
            req = self.read_body()
            removed_id = req.get("removedId", req.get("undoId"))
        except Exception as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        if mock_data_enabled():
            with MOCK_DATA_LOCK:
                for index, build in enumerate(MOCK_REMOVED_BUILDS):
                    if build["removedId"] == removed_id:
                        restored = MOCK_REMOVED_BUILDS.pop(index)
                        restored.pop("removedId", None)
                        restored.pop("removedAt", None)
                        MOCK_BUILDS.insert(0, restored)
                        return self.send_json({"ok": True})
            return self.send_json({"error": "removed build is no longer available"}, status=404)
        with REMOVED_BUILDS_WRITE_LOCK:
            removed_manifest = removed_manifest_for(removed_id)
            if removed_manifest is None:
                return self.send_json({"error": "removed build is no longer available"}, status=404)
            build_id = removed_manifest.name[len(removed_id) + 1:-5]
            destination = BUILDS_DIR / f"{build_id}.json"
            if destination.exists():
                return self.send_json({"error": "a build with this id was published again; undo will not overwrite it"}, status=409)
            try:
                removed_manifest.replace(destination)
            except OSError as e:
                return self.send_json({"error": f"could not restore build: {e}"}, status=500)
        self.send_json({"ok": True})

    def handle_record_launch(self, build_id):
        if not build_id or "/" in build_id or ".." in build_id:
            return self.send_json({"error": "bad build id"}, status=400)
        if mock_data_enabled():
            with MOCK_DATA_LOCK:
                for manifest in MOCK_BUILDS:
                    if manifest["id"] == build_id:
                        run_count = manifest.get("runCount", 0) + 1
                        last_run_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        manifest["runCount"] = run_count
                        manifest["lastRunAt"] = last_run_at
                        return self.send_json({"ok": True, "runCount": run_count, "lastRunAt": last_run_at})
            return self.send_json({"error": "unknown build"}, status=404)
        manifest_path = BUILDS_DIR / f"{build_id}.json"
        with BUILD_WRITE_LOCK:
            try:
                manifest = json.loads(manifest_path.read_text())
            except FileNotFoundError:
                return self.send_json({"error": "unknown build"}, status=404)
            except (OSError, json.JSONDecodeError) as e:
                return self.send_json({"error": f"could not read build manifest: {e}"}, status=500)
            if not isinstance(manifest, dict):
                return self.send_json({"error": "invalid build manifest"}, status=500)

            prior_count = manifest.get("runCount", 0)
            if isinstance(prior_count, bool) or not isinstance(prior_count, int) or prior_count < 0:
                prior_count = 0
            run_count = prior_count + 1
            last_run_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            manifest["runCount"] = run_count
            manifest["lastRunAt"] = last_run_at

            temporary_manifest = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=BUILDS_DIR,
                    prefix=f".{build_id}.", suffix=".tmp", delete=False,
                ) as f:
                    temporary_manifest = Path(f.name)
                    json.dump(manifest, f, indent=2)
                    f.write("\n")
                temporary_manifest.replace(manifest_path)
            except OSError as e:
                return self.send_json({"error": f"could not record launch: {e}"}, status=500)
            finally:
                if temporary_manifest is not None:
                    try:
                        temporary_manifest.unlink()
                    except FileNotFoundError:
                        pass

        self.send_json({"ok": True, "runCount": run_count, "lastRunAt": last_run_at})

    def handle_install(self):
        try:
            req = self.read_body()
            build_id, device_id = req["buildId"], req["deviceId"]
        except Exception as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        manifest = self.resolve_build(build_id)
        if not manifest:
            return self.send_json({"error": f"unknown build {build_id}"}, status=404)
        if mock_data_enabled():
            self.start_stream_response()
            try:
                self.emit(f"installing mock build {manifest['app']['name']} on {device_id}…")
                self.emit("install: OK")
                self.emit("DONE 0")
            finally:
                self.end_stream()
            return
        build_os = manifest.get("os", "ios")
        app_path = manifest["app"]["path"]
        exists = Path(app_path).is_dir() if build_os == "ios" else Path(app_path).is_file()
        if not exists:
            return self.send_json({"error": f"artifact no longer on disk: {app_path}"}, status=410)

        if build_os == "android":
            cmd = [ADB, "-s", device_id, "install", "-r", app_path]
        else:
            cmd = ["xcrun", "devicectl", "device", "install", "app",
                   "--device", device_id, app_path]

        self.start_stream_response()
        try:
            rc = self.stream_command(cmd)
            if rc is not None:
                self.emit("install: OK" if rc == 0 else f"install: FAILED (exit {rc})")
                self.emit(f"DONE {rc}")
        except Exception as e:
            try:
                self.emit(f"server error: {e}")
                self.emit("DONE 1")
            except Exception:
                pass
        finally:
            self.end_stream()

    def handle_launch(self):
        """Launch the app and stream its logs until the app exits or the client
        disconnects. iOS: devicectl --console relaunches; detach kills only the
        local devicectl via SIGKILL so the app survives. Android restarts the
        app, then attaches pid-scoped logcat; detach never touches the app."""
        try:
            req = self.read_body()
            build_id, device_id = req["buildId"], req["deviceId"]
        except Exception as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
        manifest = self.resolve_build(build_id)
        if not manifest:
            return self.send_json({"error": f"unknown build {build_id}"}, status=404)
        bundle_id = manifest["app"]["bundleId"]

        self.start_stream_response()
        try:
            if mock_data_enabled():
                self.mock_launch(device_id, bundle_id)
            elif manifest.get("os", "ios") == "android":
                self.launch_android(device_id, bundle_id)
            else:
                rc = self.stream_command([
                    "xcrun", "devicectl", "device", "process", "launch",
                    "--terminate-existing", "--console",
                    "--device", device_id, bundle_id,
                ], hard_kill=True)
                if rc is not None:
                    self.emit(f"app exited (devicectl exit {rc})")
                    self.emit(f"DONE {rc}")
        except Exception as e:
            try:
                self.emit(f"server error: {e}")
                self.emit("DONE 1")
            except Exception:
                pass
        finally:
            self.end_stream()

    def mock_launch(self, device_id, bundle_id):
        """Emit a live-looking log stream until the browser detaches."""
        self.emit(f"launching mock {bundle_id} on {device_id}…")
        self.emit("launched (mock process 4812)")
        index = 0
        try:
            while True:
                time.sleep(1)
                index += 1
                self.emit(f"{bundle_id} 4812 [mock] screen rendered · heartbeat {index}")
        except (BrokenPipeError, ConnectionResetError):
            return

    def launch_android(self, device_id, bundle_id):
        self.emit(f"launching {bundle_id}…")
        rc = self.stream_command([ADB, "-s", device_id, "shell", "am", "force-stop", bundle_id], hard_kill=True)
        if rc is not None and rc != 0:
            self.emit(f"launch: FAILED — could not stop {bundle_id} (exit {rc})")
            self.emit("DONE 1")
            return
        rc = self.stream_command([
            ADB, "-s", device_id, "shell", "monkey", "-p", bundle_id,
            "-c", "android.intent.category.LAUNCHER", "1",
        ], hard_kill=True)
        if rc is not None and rc != 0:
            self.emit(f"launch: FAILED — could not start {bundle_id} (exit {rc})")
            self.emit("DONE 1")
            return
        pid = None
        for _ in range(20):
            pid = adb(device_id, "shell", "pidof", bundle_id)
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            self.emit(f"launch: FAILED — {bundle_id} did not start (is it installed?)")
            self.emit("DONE 1")
            return
        self.emit(f"launched (pid {pid})")
        rc = self.stream_command(
            [ADB, "-s", device_id, "logcat", "--pid", pid.split()[0], "-v", "time"],
            hard_kill=True,
        )
        if rc is not None:
            self.emit(f"logcat ended (exit {rc})")
            self.emit(f"DONE {rc}")


def main():
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Mobile Build Monitor dashboard: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
