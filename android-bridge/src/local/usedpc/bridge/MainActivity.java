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
        status.setText("번장 제어 브리지 0.2 — 최대 6개 동시 터치\n\n"+
            (s==null?"접근성 권한을 먼저 켜 주세요.":s.status())+
            "\n\n실행 후 번장 관심 → 즐겨찾기로 이동하세요.\n로컬 서버가 승인한 새로고침·클릭·뒤로가기만 실행합니다.\n빨간점은 키워드 저장 확인 후 처리합니다.\n\n페어링 코드는 최초 연결할 때만 알려주세요.\n중지하려면 이 앱으로 돌아와 중지를 누르세요.");
        handler.postDelayed(this,1000);
    }};
    public void onCreate(Bundle b){super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout layout=new LinearLayout(this);layout.setOrientation(1);layout.setPadding(32,48,32,24);
        status=new TextView(this);status.setTextSize(17);layout.addView(status);
        Button permission=new Button(this);permission.setText("1. 접근성 설정 열기");
        permission.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));layout.addView(permission);
        Button start=new Button(this);start.setText("2. 실행");
        start.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.startDiagnostics();});layout.addView(start);
        Button stop=new Button(this);stop.setText("중지");
        stop.setOnClickListener(v->{if(BridgeService.instance!=null)BridgeService.instance.stopDiagnostics();});layout.addView(stop);
        setContentView(layout);
    }
    public void onResume(){super.onResume();handler.post(update);}
    public void onPause(){handler.removeCallbacks(update);super.onPause();}
}
