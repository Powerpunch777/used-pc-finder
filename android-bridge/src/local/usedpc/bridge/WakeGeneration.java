package local.usedpc.bridge;

/** Pure generation policy, shared by storage and the offline regression test. */
public final class WakeGeneration {
    public static long next(long received,long sent,long flight){
        if(received<0||sent<0||sent>received||flight<0||flight>received)throw new IllegalArgumentException("wake_state_invalid");
        if(received<=sent)return 0;
        return flight>sent?flight:received;
    }
    public static boolean acknowledges(long received,long flight,long value){
        return value>0&&value==flight&&value<=received;
    }
}
