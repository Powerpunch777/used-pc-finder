package local.usedpc.bridge;

// Pure timing contract, also exercised by JVM tests in the APK build.
final class PreflightTiming {
    static boolean canStart(long readAt,long readFinished,long started,long expires) {
        return readAt>0 && readFinished>=readAt && started>=readFinished &&
            started-readAt<=6000 && started<=expires;
    }
}
