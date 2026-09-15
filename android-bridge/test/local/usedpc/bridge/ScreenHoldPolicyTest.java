package local.usedpc.bridge;

public final class ScreenHoldPolicyTest {
    private static void check(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        ScreenHoldPolicy p=new ScreenHoldPolicy();
        check(!p.evaluate(true,true,true,false,null));
        check(p.evaluate(true,true,true,false,"kr.co.quicket"));
        for(int i=0;i<10000;i++)check(p.evaluate(true,true,true,false,null));
        check("loading_hold".equals(p.reason));
        check(!p.evaluate(true,true,true,false,"other.app"));
        check(!p.evaluate(true,true,true,false,null));
        check(p.evaluate(true,true,true,false,"local.usedpc.bridge"));
        check(!p.evaluate(true,true,false,false,null));
        check(!p.evaluate(true,true,true,false,null)); // Manual screen off resets memory.
        check(p.evaluate(true,true,true,false,"kr.co.quicket"));
        check(!p.evaluate(true,true,true,true,"kr.co.quicket"));
        check(!p.evaluate(true,false,true,false,"kr.co.quicket"));
        check(!p.evaluate(false,true,true,false,"kr.co.quicket"));
        System.out.println("ScreenHoldPolicyTest passed");
    }
}
