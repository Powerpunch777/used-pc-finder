package local.usedpc.bridge;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
public final class StopReceiver extends BroadcastReceiver {
    public void onReceive(Context c,Intent i){if(BridgeService.instance!=null)BridgeService.instance.stopDiagnostics();}
}
