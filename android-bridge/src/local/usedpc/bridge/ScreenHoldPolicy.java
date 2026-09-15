package local.usedpc.bridge;

/** Screen power only, never a permission to touch or unlock the phone. */
public final class ScreenHoldPolicy {
    private boolean targetSeen;
    public String reason="stopped";

    public boolean evaluate(boolean running,boolean enabled,boolean interactive,boolean locked,String topPackage){
        if(!running||!enabled||!interactive||locked){
            targetSeen=false;
            reason=!running?"stopped":!enabled?"disabled":!interactive?"screen_off":"locked";
            return false;
        }
        if(topPackage==null||topPackage.isEmpty()){
            // Empty accessibility trees during refresh/network loading are not
            // evidence that the user left Bunjang. Keep the previous decision.
            reason=targetSeen?"loading_hold":"waiting_for_bunjang";
            return targetSeen;
        }
        targetSeen="kr.co.quicket".equals(topPackage)||"local.usedpc.bridge".equals(topPackage);
        reason=targetSeen?"foreground_hold":"other_app";
        return targetSeen;
    }
}
