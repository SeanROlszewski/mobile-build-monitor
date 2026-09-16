# Mobile Build Monitor

A local dashboard for installing iOS and Android builds on connected devices.

Build your app, publish it, then pick a device and click **Install**. The dashboard installs the build and starts live app logs. Click **Run** to relaunch the app with a fresh log stream.

Builds can come from any worktree, so you can work on several branches—or use several coding agents—at once. Each published build includes its branch, commit, PR, and testing notes so you can tell them apart.

The dashboard handles installation and launch. Build the app using your project’s usual tools.

## Requirements

- macOS with Python 3.9 or later. No Python packages to install.
- For iOS: Xcode command line tools (`xcrun devicectl`), a device paired with your Mac, and a development build signed for that device.
- For Android: `adb` on your `PATH` or in `~/Library/Android/sdk/platform-tools`, plus `aapt2` for publishing. The publisher looks for `aapt2` in `~/Library/Android/sdk/build-tools`.

## Setup

```bash
git clone git@github.com:SeanROlszewski/mobile-build-monitor.git
cd mobile-build-monitor
bin/dashboard
```

Open [localhost:8484](http://localhost:8484) to see your builds and connected devices. Use the iOS/Android switcher to choose a platform.

The server listens on `127.0.0.1`. To use a different port, pass it as the first argument:

```bash
bin/dashboard 8485
```

### Add the tools to a stable location

These optional symlinks let you use the tools from any app worktree:

```bash
mkdir -p ~/.mobile-build-monitor/bin
ln -sf "$PWD/bin/publish-build" ~/.mobile-build-monitor/bin/publish-build
ln -sf "$PWD/bin/dashboard" ~/.mobile-build-monitor/bin/dashboard
```

The examples below use these paths.

## Publishing a build

### iOS

From your app’s worktree, build for a physical device using your project’s build command. For example:

```bash
make build DESTINATION="generic/platform=iOS" > /tmp/ios_build_device.txt 2>&1
grep -c "BUILD SUCCEEDED" /tmp/ios_build_device.txt
```

Then publish the build:

```bash
~/.mobile-build-monitor/bin/publish-build \
  --scheme "ExampleApp Staging" \
  --notes "PROJ-1234: verify the updated onboarding screen" \
  --pr "https://github.com/example-org/example-ios-app/pull/1234"
```

The publisher finds the `.app` in DerivedData for the current worktree.

### Android

From your app’s worktree, assemble the variant you want to test:

```bash
./gradlew :app:assembleStagingDebug > /tmp/android_build.txt 2>&1
grep -c "BUILD SUCCESSFUL" /tmp/android_build.txt
```

Then publish the build:

```bash
~/.mobile-build-monitor/bin/publish-build --os android \
  --notes "PROJ-1234: verify the updated onboarding screen" \
  --pr "https://github.com/example-org/example-android-app/pull/1234"
```

The publisher finds the newest APK under `*/build/outputs/apk`, skipping `-androidTest.apk` files. It reads app metadata with `aapt2 dump badging`.

You can pass `--scheme` to name the build, or leave it out to use the Gradle variant inferred from the APK path, such as `stagingDebug`.

### Options

| Option | Description |
|---|---|
| `--worktree PATH` | App worktree to publish from. Defaults to the Git root of the current directory. |
| `--app-path PATH` | Use a specific `.app` directory or `.apk` file instead of finding one automatically. |
| `--scheme NAME` | Xcode scheme on iOS. On Android, defaults to the Gradle variant inferred from the APK path. |
| `--configuration NAME` | Build configuration to record. For iOS, inferred from the products directory name (`<Config>-iphoneos`) when omitted. |
| `--notes TEXT` | Testing notes shown on the build card. |
| `--pr URL` | Link to the pull request. |

Include testing notes and a PR link when you can. They make it easier to identify builds and remember what to check.

### Build validation

The publisher checks builds before adding them to the dashboard:

- **iOS simulator builds** are rejected. Build for a physical device instead.
- **iOS builds without arm64 support or an embedded provisioning profile** are rejected.
- **Unreadable Android APKs** are rejected if `aapt2` cannot parse them. Non-debuggable APKs produce a warning.
- **Builds older than the worktree’s latest commit** produce a warning. Rebuild if you’re unsure whether the build includes your changes.

If an iOS build fails because of signing, use your project’s documented Xcode build command. You may need provisioning updates enabled, depending on your signing setup.

## Managing builds

Published builds are listed in `~/.mobile-build-monitor/builds/`, with one JSON manifest per build. To use another directory, set `MOBILE_BUILD_MONITOR_DIR` for both the publisher and the dashboard.

Removing a build moves its manifest to the collapsed **Recently Removed** section and leaves the `.app` or `.apk` on disk. You can restore it there, or permanently remove its manifest; neither action deletes the build artifact.

Keep the app file on disk until you’ve installed it. Publishing records its location; it does not copy the build.

## Using coding agents

The included `publish-device-build` skill covers building and publishing device builds. It uses `publish-build` to find the build, validate it, collect app and Git metadata, and write the manifest. Multiple agents can publish from separate worktrees at the same time.

### Codex

Codex discovers the checked-in skill automatically when working in this repository. To make it available while Codex works in any app worktree, install it globally from this repository’s root:

```bash
mkdir -p ~/.agents/skills
ln -sfn "$PWD/skills/publish-device-build" ~/.agents/skills/publish-device-build
```

Start a new Codex session if the skill does not appear immediately.

### Claude Code

```bash
mkdir -p ~/.claude/skills
ln -sfn "$PWD/skills/publish-device-build" ~/.claude/skills/publish-device-build
```

## Repository layout

```text
bin/publish-build   Validates builds and publishes their manifests
bin/dashboard       Starts the local dashboard
dashboard/          Python server and web interface
.agents/skills/     Codex skill entry point
skills/             Shared skill instructions for Codex and Claude Code
```

The server uses only the Python standard library.

## Manifest format

The publisher writes one file per build to the builds directory, named `<id>.json`. Use the publisher whenever possible. If you need to write a manifest yourself, follow the format and validation rules below.

```json
{
  "schemaVersion": 1,
  "id": "seanolszewski-my-branch_20260904T031500Z",
  "os": "ios",
  "builtAt": "2026-09-04T03:12:41Z",
  "publishedAt": "2026-09-04T03:15:00Z",
  "scheme": "ExampleApp Staging",
  "configuration": "Staging_Debug",
  "platform": "iphoneos",
  "app": {
    "name": "Example App",
    "bundleId": "com.example.app.staging",
    "version": "4.0.2600",
    "buildNumber": "1",
    "path": "/abs/path/to/Example App.app"
  },
  "source": {
    "worktree": "/abs/path/to/worktree",
    "branch": "feature/my-branch",
    "commit": "abc1234",
    "pr": "https://github.com/example-org/example-ios-app/pull/1234"
  },
  "notes": "Verify the updated onboarding screen."
}
```

### Fields

| Field | Required | Description |
|---|---|---|
| `schemaVersion` | Yes | Must be `1`. A new version requires a corresponding dashboard update. |
| `id` | Yes | Unique build ID and filename stem, using `slug(branch)_UTCstamp`. |
| `os` | Yes | `"ios"` or `"android"`. Older manifests without this field are treated as iOS builds. |
| `builtAt` | Yes | Build time in ISO-8601 UTC, taken from the executable or APK modification time. |
| `publishedAt` | No | Time the manifest was written, in ISO-8601 UTC. Older manifests fall back to `builtAt`, which previously stored the publish time. |
| `scheme` | Yes | Xcode scheme on iOS; Gradle variant on Android. |
| `configuration` | Yes | iOS build configuration, such as `Staging_Debug`; variant on Android. |
| `platform` | Yes | `iphoneos` for iOS or `android` for Android. |
| `app.name` | Yes | `CFBundleDisplayName` or `CFBundleName` on iOS; `application-label` on Android. |
| `app.bundleId` | Yes | `CFBundleIdentifier` on iOS; `applicationId` on Android. Used to launch the app and collect logs. |
| `app.version` | Yes | `CFBundleShortVersionString` on iOS; `versionName` on Android. |
| `app.buildNumber` | Yes | `CFBundleVersion` on iOS; `versionCode` on Android. |
| `app.path` | Yes | Absolute path to the `.app` directory or `.apk` file. iOS `.ipa` files are not supported. |
| `source.worktree` | Yes | Absolute path to the worktree that produced the build. |
| `source.branch` | Yes | Git branch name. |
| `source.commit` | Yes | Short Git commit hash. |
| `source.pr` | No | Pull request URL, or `null`. |
| `notes` | No | Testing notes shown on the build card. |

Manually published iOS builds must pass the same device-platform, arm64, code-signature, and provisioning-profile checks as builds published by the script.

## Platform details

### iOS

Installation uses `xcrun devicectl device install app`. Logs come from `devicectl device process launch --terminate-existing --console`, which can attach only when the app launches. That’s why **Run** relaunches the app.

To stop collecting logs without terminating the app, the server kills the local `devicectl` process with `SIGKILL`. Catchable signals such as `SIGTERM` would be forwarded to the app.

The server checks device availability with `device info details`. The `tunnelState` reported by `devicectl list devices` tracks the wireless tunnel and can be stale for wired devices.

### Android

Installation uses `adb install -r`. Logs use `logcat`, filtered to the app’s process.

Logging can attach to a running app without restarting it. If the app isn’t running, it is started through a launcher intent. Stopping the log stream leaves the app running.
