---
name: publish-device-build
description: Build a device-installable mobile build (iOS .app from example-ios-app, Android .apk from example-android-app) in the current worktree and publish it to the local Mobile App Builds dashboard so the user can install it on a phone. Use when the user says "put this on my phone", "publish a build", "deliver this build to my device", "I want to QA this on device", or after finishing feature work that needs on-device verification.
---

# Publish a device build to the Mobile App Builds dashboard

The user QAs agent work by installing builds on a physical phone from a local
dashboard (http://localhost:8484). Your job ends when the build is **published**
— you never install to or launch on the device yourself; the user does that
with a button.

The full manifest contract and troubleshooting notes live in the
[mobile-build-monitor](https://github.com/SeanROlszewski/mobile-build-monitor)
README (also symlinked at `~/.mobile-build-monitor/README.md` on a standard install).
Pick the platform by the repo you are working in and follow the steps from the
worktree root.

## iOS (example-ios-app)

### 1. Build for device

Simulator builds cannot be sideloaded. Override the destination (device builds
of this repo are otherwise identical to `make build`):

```bash
make build DESTINATION="generic/platform=iOS" > /tmp/ios_build_<task>_device.txt 2>&1
```

- Example ID app instead: `make build DESTINATION="generic/platform=iOS"`.
- Use a literal, task-specific log filename (no shell variables/expansions).
- 10-minute Bash timeout or run in background; then confirm success:

```bash
grep -c "BUILD SUCCEEDED" /tmp/ios_build_<task>_device.txt
```

If the build fails on signing/provisioning, retry once letting Xcode refresh
profiles:

```bash
make build XCODEBUILD_BASE='xcodebuild -project "ExampleApp.xcodeproj" -destination "generic/platform=iOS" -skipMacroValidation -allowProvisioningUpdates' > /tmp/ios_build_<task>_device2.txt 2>&1
```

### 2. Publish

```bash
python3 ~/.mobile-build-monitor/bin/publish-build \
  --scheme "ExampleApp Staging" \
  --notes "<ticket>: <one line saying what to QA in this build>" \
  --pr "<PR URL if one exists>"
```

- `--scheme` must be the scheme you built (`"ExampleID Staging"` for Example ID).
- The script auto-discovers the `.app` via this worktree's DerivedData and
  validates it (device platform, arm64, signature, provisioning profile). If
  discovery picks the wrong product, pass `--app-path` explicitly.

## Android (example-android-app)

### 1. Assemble the QA variant

```bash
./gradlew :app:assembleStagingDebug > /tmp/android_build_<task>.txt 2>&1
grep -c "BUILD SUCCESSFUL" /tmp/android_build_<task>.txt
```

Use a literal, task-specific log filename. Other variants
(`assembleDevDebug`, etc.) are fine when the task calls for them.

### 2. Publish

```bash
python3 ~/.mobile-build-monitor/bin/publish-build --os android \
  --notes "<ticket>: <one line saying what to QA in this build>" \
  --pr "<PR URL if one exists>"
```

- Auto-discovers the newest APK under `*/build/outputs/apk` in the worktree
  (ignoring `-androidTest.apk`); pass `--app-path` to override.
- `--scheme` is optional — it defaults to the Gradle variant (e.g.
  `stagingDebug`).

## 3. Report (both platforms)

Publishing succeeded when the script prints `published: ...`. Surface any
`warning:` lines to the user. Tell the user the build is published and
installable from http://localhost:8484 (their platform switcher: iOS/Android).
If the dashboard may not be running, mention: `~/.mobile-build-monitor/bin/dashboard`.

## Rules

- Do NOT clean DerivedData / Gradle build outputs or delete the worktree after
  publishing — the manifest points at the artifact on disk; deleting it breaks
  Install.
- Do NOT run `devicectl`/`adb` install or launch yourself; delivery is
  user-initiated.
- Do NOT hand-write manifest JSON unless the publisher script itself is
  broken; if you must, follow the contract in `~/.mobile-build-monitor/README.md`
  exactly (`schemaVersion: 1`, correct `os` field).
