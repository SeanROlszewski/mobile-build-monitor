# Mobile Build Monitor

Local sideload delivery for World mobile builds. Agents (or humans) build
device-installable artifacts in their worktrees — iOS `.app`s from
`world-app-ios`, Android `.apk`s from `wld-android` — and **publish** them to a
local queue. A zero-dependency web dashboard lists published builds and
connected devices (iOS ⇄ Android switcher); clicking **Install** runs
`xcrun devicectl device install app` / `adb install -r` against the selected
device, and **Run** relaunches the app and streams its logs live.

The dashboard never builds anything — it is install/launch-only. Builds are
produced in worktrees and handed over via a manifest contract, so any number of
agents can publish concurrently without touching each other.

```
bin/publish-build   publisher: validates a build and writes its manifest
bin/dashboard       starts the dashboard server (http://localhost:8484)
dashboard/          server.py (python3 stdlib only) + index.html
skills/             Claude Code agent skill for publishing builds
```

## Requirements

- macOS with Python 3.9+ (stock `python3` is fine — no pip packages needed)
- iOS installs: Xcode command line tools (`xcrun devicectl`), a device paired
  with your Mac, and builds signed with a development profile that includes it
- Android installs: `adb` (on `PATH` or in `~/Library/Android/sdk/platform-tools`),
  plus `aapt2` for publishing (auto-found under `~/Library/Android/sdk/build-tools`)

## Setup

```bash
git clone git@github.com:SeanROlszewski/mobile-build-monitor.git
cd mobile-build-monitor
bin/dashboard          # → http://localhost:8484
```

Published manifests live outside the repo in `~/.world-builds/builds/` (one
JSON file per build). Override the data directory with the `WORLD_BUILDS_DIR`
environment variable — both the publisher and the server honor it. The server
binds 127.0.0.1 only; pass a port as the first argument to change it from 8484.

Optionally symlink the tools somewhere stable so agent instructions don't
depend on where you cloned the repo:

```bash
mkdir -p ~/.world-builds/bin
ln -sf "$PWD/bin/publish-build" ~/.world-builds/bin/publish-build
ln -sf "$PWD/bin/dashboard" ~/.world-builds/bin/dashboard
```

## Publishing a build

iOS (`world-app-ios`), from the worktree root:

```bash
# 1. Build for device (NOT simulator). Redirect output — it will be truncated otherwise.
make build-world DESTINATION="generic/platform=iOS" > /tmp/ios_build_device.txt 2>&1
grep -c "BUILD SUCCEEDED" /tmp/ios_build_device.txt

# 2. Publish (auto-discovers the .app via DerivedData for this worktree)
bin/publish-build \
  --scheme "WorldApp Staging" \
  --notes "MCORE-1234: verify the new selfie error screen" \
  --pr "https://github.com/worldcoin/world-app-ios/pull/1234"
```

World ID variant: `make build-id DESTINATION="generic/platform=iOS"` and
`--scheme "WorldID Staging"`.

Android (`wld-android`), from the worktree root:

```bash
# 1. Assemble the QA variant. Redirect output — it will be truncated otherwise.
./gradlew :app:assembleStagingDebug > /tmp/android_build.txt 2>&1
grep -c "BUILD SUCCESSFUL" /tmp/android_build.txt

# 2. Publish (auto-discovers the newest APK under */build/outputs/apk)
bin/publish-build --os android \
  --notes "APP-1234: verify the new selfie error screen" \
  --pr "https://github.com/worldcoin/wld-android/pull/1234"
```

`--scheme` is optional on Android — it defaults to the Gradle variant inferred
from the APK path (e.g. `stagingDebug`). Metadata comes from `aapt2 dump
badging`; `-androidTest.apk` artifacts are ignored during discovery.

Publisher options:

- `--worktree PATH` — defaults to the git toplevel of the current directory.
- `--app-path PATH` — skip auto-discovery and use this `.app`/`.apk` explicitly.
- `--configuration NAME` — recorded in the manifest; inferred from the
  products dir name (`<Config>-iphoneos`) when omitted.
- `--notes` / `--pr` — strongly encouraged; this is what the human sees.

Failure modes the publisher enforces:

- **Simulator build** (`DTPlatformName != iphoneos`): rejected. Rebuild with
  `DESTINATION="generic/platform=iOS"`.
- **No arm64 slice** or **missing `embedded.mobileprovision`** (iOS): rejected —
  the build can't be sideloaded.
- **Unreadable APK** (Android): rejected if `aapt2 dump badging` can't parse
  it; warns if the APK isn't debuggable (release-style build).
- **Stale artifact**: if the newest `.app`/`.apk` is older than the worktree's
  last commit, the publisher warns; rebuild if in doubt.
- **Signing failures during the iOS build**: retry the make command with
  `XCODEBUILD_BASE='xcodebuild -project "WorldApp.xcodeproj" -destination "generic/platform=iOS" -skipMacroValidation -allowProvisioningUpdates'`
  so Xcode can refresh the development provisioning profiles.

## Agent integration

`skills/publish-device-build/SKILL.md` is a Claude Code skill that teaches an
agent the full build-and-publish flow. Install it by symlinking into your
personal skills directory:

```bash
ln -s "$PWD/skills/publish-device-build" ~/.claude/skills/publish-device-build
```

Agents should **always publish through `bin/publish-build`** — it locates the
artifact, validates the contract (device platform, arm64, code signature,
embedded provisioning profile), extracts Info.plist/git metadata, and writes
the manifest. Hand-writing manifest JSON is only a fallback if the script
cannot run; if you do, every "yes" field below is mandatory and the same
validation rules apply.

## Build manifest contract (schemaVersion 1)

One JSON file per build in the builds directory, named `<id>.json`.

```json
{
  "schemaVersion": 1,
  "id": "seanolszewski-my-branch_20260904T031500Z",
  "os": "ios",
  "builtAt": "2026-09-04T03:12:41Z",
  "publishedAt": "2026-09-04T03:15:00Z",
  "scheme": "WorldApp Staging",
  "configuration": "Staging_Debug",
  "platform": "iphoneos",
  "app": {
    "name": "World App",
    "bundleId": "org.worldcoin.insight.staging",
    "version": "4.0.2600",
    "buildNumber": "1",
    "path": "/abs/path/to/World App.app"
  },
  "source": {
    "worktree": "/abs/path/to/worktree",
    "branch": "seanolszewski/my-branch",
    "commit": "abc1234",
    "dirty": false,
    "pr": "https://github.com/worldcoin/world-app-ios/pull/1234"
  },
  "notes": "What to QA and why this build exists."
}
```

Field rules:

| Field | Required | Notes |
|---|---|---|
| `schemaVersion` | yes | Literal `1`. Bump only with a dashboard change. |
| `id` | yes | Unique; also the filename stem. `slug(branch)_UTCstamp`. |
| `os` | yes | `"ios"` or `"android"`. Missing = `"ios"` (pre-Android manifests). |
| `builtAt` | yes | ISO-8601 UTC. When the artifact was **built** (executable/APK mtime). |
| `publishedAt` | no | ISO-8601 UTC. When the manifest was written. Missing = older manifests; treat as `builtAt` (which was publish time before this field existed). |
| `scheme` | yes | iOS: Xcode scheme. Android: Gradle variant (e.g. `stagingDebug`). |
| `configuration` | yes | iOS: build configuration (e.g. `Staging_Debug`). Android: variant. |
| `platform` | yes | iOS: `iphoneos` (simulator builds rejected). Android: `android`. |
| `app.name` | yes | iOS: `CFBundleDisplayName`/`CFBundleName`. Android: `application-label`. |
| `app.bundleId` | yes | iOS: `CFBundleIdentifier`. Android: `applicationId`. Used for launch/logs. |
| `app.version` / `app.buildNumber` | yes | iOS: Info.plist. Android: `versionName` / `versionCode`. |
| `app.path` | yes | iOS: absolute path to the `.app` **directory** (not an .ipa). Android: absolute path to the `.apk` file. Must stay on disk until installed — don't clean build output before QA. |
| `source.worktree` | yes | Absolute path to the worktree that built it. |
| `source.branch` / `source.commit` | yes | `commit` is the short hash. |
| `source.dirty` | yes | `true` if the worktree had uncommitted changes. |
| `source.pr` | no | PR URL if one exists, else `null`. |
| `notes` | no | Free text shown on the dashboard card — say what to QA. |

## Platform notes

- iOS install = `devicectl device install app`; app logs = `devicectl device
  process launch --terminate-existing --console` (console attach is only
  possible at launch, so **Run** relaunches the app). Detaching kills the local
  `devicectl` with SIGKILL — devicectl forwards catchable signals to the app,
  so SIGTERM would terminate the app on the device.
- `devicectl list devices` reports a stale `tunnelState` for wired devices (it
  tracks the wireless tunnel), so the server probes each device with
  `device info details` for live state.
- Android install = `adb install -r`; logs = pid-scoped `logcat`, which
  attaches to the running app **without** restarting it (the app is launched
  via a LAUNCHER intent only if not already running). Stop is always safe.
