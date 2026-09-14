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

public class MainActivity extends Activity {
    private final Handler handler=new Handler();
    private TextView status;
    private final Runnable update=new Runnable(){public void run(){
        BridgeService s=BridgeService.instance;
        status.setText("진단 앱 0.1 — 라이브 자동화 전환 전 시험용\n\n"+
            (s==null?"접근성 권한을 먼저 켜 주세요.":s.status())+
            "\n\n진단 시작 후 번장 즐겨찾기로 이동하세요.\n화면 읽기만 시작하며, 카톡 발송이나 빨간점 제거는 하지 않습니다.\n\n페어링 코드를 채팅에 알려주시면 이 로컬 서버와 연결합니다.\n중지하려면 이 앱으로 돌아와 중지를 누르세요.");
        handler.postDelayed(this,1000);
    }};
    public void onCreate(Bundle b){super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout layout=new LinearLayout(this);layout.setOrientation(1);layout.setPadding(32,48,32,24);
        status=new TextView(this);status.setTextSize(17);layout.addView(status);
        Button permission=new Button(this);permission.setText("1. 접근성 설정 열기");
        permission.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));layout.addView(permission);
        Button start=new Button(this);start.setText("2. 진단 시작 (읽기 전용)");
        start.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.startDiagnostics();});layout.addView(start);
        Button stop=new Button(this);stop.setText("중지");
        stop.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.stopDiagnostics();});layout.addView(stop);
        setContentView(layout);
    }
    public void onResume(){super.onResume();handler.post(update);}
    public void onPause(){handler.removeCallbacks(update);super.onPause();}
}
