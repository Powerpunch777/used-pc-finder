# Android bridge build

This branch adds only the Android client and its build workflow. It contains no
production database, logs, phone screenshots, account credentials or signing key.

The first APK is diagnostic: manual Start, accessibility permission granted by
the user, localhost-only pairing, Bunjang screen reading. It is not yet a tested
replacement for the live worker. Installation and real-device testing remain.

GitHub Actions uses the hosted Ubuntu runner's Java/Android SDK. It produces an
**unsigned** APK and the official Android signing CLI converted to DEX. The phone
downloads this small artifact and runs the signing CLI with its existing Android
runtime and persistent local private key. The signing key never leaves the phone.
The APK is published locally only after signature verification succeeds.

The workflow has read-only repository permissions, pinned action revisions and
seven-day artifact retention. It has no deployment credentials, arbitrary
remote-execution endpoint, release publishing or third-party analytics.
