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
  POST   /api/builds/<id>/run  records an explicit dashboard run
  DELETE /api/builds/<id>  soft-remove a manifest (leaves the artifact on disk)
  DELETE /api/removed-builds/<id>  permanently delete a removed manifest
  POST   /api/builds/restore  restore a recently removed manifest

Binds 127.0.0.1 only. Zero dependencies (python3 stdlib).
"""

import datetime
import json
import os
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
BUILD_WRITE_LOCK = threading.Lock()
INDEX = Path(__file__).parent / "index.html"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8484

ADB = shutil.which("adb") or str(Path.home() / "Library/Android/sdk/platform-tools/adb")


def load_builds():
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
            self.send_json({"buildsDirectory": str(BUILDS_DIR)})
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
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
        manifest = BUILDS_DIR / f"{build_id}.json"
        if not manifest.is_file():
            return self.send_json({"error": "unknown build"}, status=404)
        # Keep the manifest outside the active list. The restore endpoint moves
        # this exact file back without trusting the browser to reconstruct it.
        REMOVED_BUILDS_DIR.mkdir(parents=True, exist_ok=True)
        removed_id = uuid.uuid4().hex
        removed_manifest = REMOVED_BUILDS_DIR / f"{removed_id}-{build_id}.json"
        try:
            manifest.replace(removed_manifest)
        except OSError as e:
            return self.send_json({"error": f"could not remove build: {e}"}, status=500)
        self.send_json({"ok": True, "removedId": removed_id, "buildId": build_id})

    def handle_hard_delete(self, removed_id):
        removed_manifest = removed_manifest_for(removed_id)
        if removed_manifest is None:
            return self.send_json({"error": "unknown removed build"}, status=404)
        try:
            removed_manifest.unlink()
        except OSError as e:
            return self.send_json({"error": f"could not permanently delete build: {e}"}, status=500)
        self.send_json({"ok": True})

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def resolve_build(self, build_id):
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
        if self.path == "/api/builds/restore":
            return self.handle_restore_build()
        prefix, suffix = "/api/builds/", "/run"
        if self.path.startswith(prefix) and self.path.endswith(suffix):
            return self.handle_record_run(self.path[len(prefix):-len(suffix)])
        if self.path == "/api/install":
            return self.handle_install()
        if self.path == "/api/launch":
            return self.handle_launch()
        self.send_json({"error": "not found"}, status=404)

    def handle_restore_build(self):
        try:
            req = self.read_body()
            removed_id = req.get("removedId", req.get("undoId"))
        except Exception as e:
            return self.send_json({"error": f"bad request: {e}"}, status=400)
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

    def handle_record_run(self, build_id):
        if not build_id or "/" in build_id or ".." in build_id:
            return self.send_json({"error": "bad build id"}, status=400)
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
                return self.send_json({"error": f"could not record run: {e}"}, status=500)
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
        disconnects. iOS: devicectl --console (relaunches; detach kills only the
        local devicectl via SIGKILL so the app survives). Android: attaches
        pid-scoped logcat to the running app, launching it first if needed —
        no restart, and detach never touches the app."""
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
            if manifest.get("os", "ios") == "android":
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

    def launch_android(self, device_id, bundle_id):
        pid = adb(device_id, "shell", "pidof", bundle_id)
        if pid:
            self.emit(f"app already running (pid {pid}); attaching logcat without restart")
        else:
            self.emit(f"launching {bundle_id}…")
            adb(device_id, "shell", "monkey", "-p", bundle_id,
                "-c", "android.intent.category.LAUNCHER", "1", timeout=30)
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
