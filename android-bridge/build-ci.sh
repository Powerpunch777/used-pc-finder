#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bridge_sdk="${ANDROID_SDK_ROOT:-${ANDROID_HOME:?Android SDK is required}}"
bridge_tools="$bridge_sdk/build-tools/35.0.0"
bridge_platform="$bridge_sdk/platforms/android-35/android.jar"
bridge_work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/bunjang-bridge-ci.XXXXXX")
bridge_out="$PWD/android-bridge/artifacts"
test -f "$bridge_platform"
for command in aapt d8 zipalign apksigner;do test -x "$bridge_tools/$command";done
mkdir -p "$bridge_work/classes" "$bridge_work/generated" "$bridge_work/dex" "$bridge_out"
javac --release 8 -d "$bridge_work/timing-tests" android-bridge/src/local/usedpc/bridge/PreflightTiming.java android-bridge/test/local/usedpc/bridge/PreflightTimingTest.java
java -cp "$bridge_work/timing-tests" local.usedpc.bridge.PreflightTimingTest
javac --release 8 -d "$bridge_work/wake-tests" android-bridge/src/local/usedpc/bridge/WakeGeneration.java android-bridge/test/local/usedpc/bridge/WakeGenerationTest.java
java -cp "$bridge_work/wake-tests" local.usedpc.bridge.WakeGenerationTest
javac --release 8 -d "$bridge_work/screen-tests" android-bridge/src/local/usedpc/bridge/ScreenHoldPolicy.java android-bridge/test/local/usedpc/bridge/ScreenHoldPolicyTest.java
java -cp "$bridge_work/screen-tests" local.usedpc.bridge.ScreenHoldPolicyTest
"$bridge_tools/aapt" package -f -m -M android-bridge/AndroidManifest.xml -S android-bridge/res -I "$bridge_platform" -J "$bridge_work/generated" -F "$bridge_work/resources.apk"
javac -encoding UTF-8 --release 8 -classpath "$bridge_platform" -d "$bridge_work/classes" android-bridge/src/local/usedpc/bridge/*.java "$bridge_work/generated/local/usedpc/bridge/R.java"
jar cf "$bridge_work/classes.jar" -C "$bridge_work/classes" .
"$bridge_tools/d8" --min-api 26 --lib "$bridge_platform" --output "$bridge_work/dex" "$bridge_work/classes.jar"
cp "$bridge_work/resources.apk" "$bridge_work/unsigned.apk"
(cd "$bridge_work/dex" && zip -q "$bridge_work/unsigned.apk" classes.dex)
"$bridge_tools/zipalign" -f -p 4 "$bridge_work/unsigned.apk" "$bridge_out/bunjang-bridge-0.5-unsigned.apk"
# Build the official signing CLI for the phone's existing Android runtime.
# This keeps the persistent signing key on the phone and avoids a local JDK.
"$bridge_tools/d8" --min-api 26 --lib "$bridge_platform" --output "$bridge_out/apksigner-android.zip" "$bridge_tools/lib/apksigner.jar"
# Deliberately unsigned: the phone signs with a persistent PRIVATE key, never
# uploaded to GitHub, put in a workflow secret, log or artifact.
"$bridge_tools/aapt" dump badging "$bridge_out/bunjang-bridge-0.5-unsigned.apk" > "$bridge_out/package-info.txt"
(cd "$bridge_out" && sha256sum bunjang-bridge-0.5-unsigned.apk apksigner-android.zip > SHA256SUMS)
printf 'Built from %s\n' "${GITHUB_SHA:-local}" > "$bridge_out/source-commit.txt"
