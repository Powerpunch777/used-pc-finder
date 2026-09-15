package local.usedpc.bridge;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.UUID;

/** Bounded durable generation counter. Never stores notification text or keys. */
public final class NativeWakeQueue {
    private static SharedPreferences prefs(Context c){return c.getSharedPreferences("native_wakes",Context.MODE_PRIVATE);}
    public static synchronized void signal(Context context){
        SharedPreferences p=prefs(context);
        long received=p.getLong("received",0);
        if(received==Long.MAX_VALUE)throw new IllegalStateException("wake_counter_full");
        if(!p.edit().putLong("received",received+1).commit())throw new IllegalStateException("wake_not_saved");
        BridgeService s=BridgeService.instance;if(s!=null)s.wakeChanged();
    }
    public static synchronized long next(Context c){
        SharedPreferences p=prefs(c);
        long sent=p.getLong("sent",0),received=p.getLong("received",0),flight=p.getLong("flight",0);
        long next=WakeGeneration.next(received,sent,flight);
        if(next==0||next==flight)return next;
        if(!p.edit().putLong("flight",next).commit())throw new IllegalStateException("wake_not_saved");
        return next;
    }
    public static synchronized String id(Context c,long generation){
        SharedPreferences p=prefs(c);String identity=p.getString("identity","");
        if(identity.isEmpty()){
            identity=UUID.randomUUID().toString().replace("-","");
            if(!p.edit().putString("identity",identity).commit())throw new IllegalStateException("wake_not_saved");
        }
        return "native_"+identity+"_"+generation;
    }
    public static synchronized void acknowledge(Context c,long generation){
        SharedPreferences p=prefs(c);
        if(!WakeGeneration.acknowledges(p.getLong("received",0),p.getLong("flight",0),generation))throw new IllegalStateException("wake_ack_mismatch");
        if(!p.edit().putLong("sent",generation).putLong("flight",0).commit())throw new IllegalStateException("wake_ack_not_saved");
    }
    public static synchronized long pending(Context c){SharedPreferences p=prefs(c);return p.getLong("received",0)-p.getLong("sent",0);}
}
