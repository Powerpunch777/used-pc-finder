package local.usedpc.bridge;

import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

public final class BunjangNotificationListener extends NotificationListenerService {
    public static volatile boolean connected=false;
    public static volatile String error="";
    public void onListenerConnected(){connected=true;signal();}
    public void onListenerDisconnected(){connected=false;}
    public void onDestroy(){connected=false;super.onDestroy();}
    public void onNotificationPosted(StatusBarNotification notification){
        // Package check BEFORE any access to data. All other notifications ignored.
        // Grouped and single notifications both wake the existing badge scanner.
        if(notification!=null&&"kr.co.quicket".equals(notification.getPackageName()))signal();
    }
    private void signal(){try{NativeWakeQueue.signal(this);error="";}catch(RuntimeException e){error="알림 신호 저장 실패";}}
}
