package local.usedpc.bridge;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.content.Intent;
import android.provider.Settings;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Switch;
import android.widget.ScrollView;

public class MainActivity extends Activity {
    private final Handler handler=new Handler();
    private TextView status;
    private final Runnable update=new Runnable(){public void run(){
        BridgeService s=BridgeService.instance;
        status.setText("번장 제어 브리지 0.5 — 화면 유지 보완\n\n"+
            (s==null?"접근성 권한을 먼저 켜 주세요.":s.status())+
            "\n\n실행 후 번장 관심 → 즐겨찾기로 이동하세요.\n시작 시 1회 확인, 이후 새 알림마다 처리합니다.\n서버 저장 확인 후 최대 6개씩 클릭합니다.\n\n번장 알림 발생 여부만 감지하며 내용은 읽지 않습니다.\n다른 앱 알림은 무시합니다.\n중지는 앱·실행 알림·빠른 설정 타일에서 가능합니다.");
        handler.postDelayed(this,1000);
    }};
    public void onCreate(Bundle b){super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout layout=new LinearLayout(this);layout.setOrientation(1);layout.setPadding(32,48,32,24);
        status=new TextView(this);status.setTextSize(17);layout.addView(status);
        Button permission=new Button(this);permission.setText("1. 접근성 설정 열기");
        permission.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));layout.addView(permission);
        Button notifications=new Button(this);notifications.setText("2. 알림 접근 권한 설정");
        notifications.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));layout.addView(notifications);
        Switch keep=new Switch(this);keep.setText("실행 중 번장 화면 유지 (배터리 소모 주의)");
        keep.setChecked(getSharedPreferences("bridge",MODE_PRIVATE).getBoolean("keep_screen",true));
        keep.setOnCheckedChangeListener((button,value)->getSharedPreferences("bridge",MODE_PRIVATE).edit().putBoolean("keep_screen",value).apply());layout.addView(keep);
        Button start=new Button(this);start.setText("3. 실행 — 지금부터 확인");
        start.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.startDiagnostics();});layout.addView(start);
        Button stop=new Button(this);stop.setText("중지");
        stop.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.stopDiagnostics();});layout.addView(stop);
        ScrollView scroll=new ScrollView(this);scroll.addView(layout);setContentView(scroll);
    }
    public void onResume(){super.onResume();handler.post(update);}
    public void onPause(){handler.removeCallbacks(update);super.onPause();}
}
