package local.usedpc.bridge;
public final class WakeGenerationTest {
    private static void check(boolean value){if(!value)throw new AssertionError("wake generation");}
    public static void main(String[] args){
        check(WakeGeneration.next(0,0,0)==0);
        check(WakeGeneration.next(5,0,0)==5); // coalesce five simultaneous signals
        check(WakeGeneration.next(8,0,5)==5); // retry immutable request after three more arrive
        check(WakeGeneration.acknowledges(8,5,5));
        check(!WakeGeneration.acknowledges(8,5,8)); // no accidental ACK of new arrivals
        check(WakeGeneration.next(8,5,0)==8);
        check(WakeGeneration.next(8,8,0)==0);
        check(!WakeGeneration.acknowledges(8,0,0));
        boolean rejected=false;
        try{WakeGeneration.next(2,3,0);}catch(IllegalArgumentException e){rejected=true;}
        check(rejected);
        System.out.println("Wake generation tests passed");
    }
}
