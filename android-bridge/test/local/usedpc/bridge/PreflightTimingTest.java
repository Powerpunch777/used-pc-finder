package local.usedpc.bridge;
public final class PreflightTimingTest {
    private static void check(boolean ok){if(!ok)throw new AssertionError("native preflight timing");}
    public static void main(String[] args){
        // Old two reads used one 5.5s budget. The second read now proves itself.
        check(PreflightTiming.canStart(4200,7020,7026,15000));
        check(PreflightTiming.canStart(1000,7000,7000,15000));
        check(!PreflightTiming.canStart(1000,7001,7001,15000));
        check(!PreflightTiming.canStart(4200,7020,7026,7025));
        check(!PreflightTiming.canStart(4200,7100,7026,15000));
        check(!PreflightTiming.canStart(0,1000,1000,15000));
        check(!PreflightTiming.canStart(4200,4199,4500,15000));
        System.out.println("Native preflight timing: 7 checks passed");
    }
}
