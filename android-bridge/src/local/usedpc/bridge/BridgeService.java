package local.usedpc.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.KeyguardManager;
import android.content.SharedPreferences;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayDeque;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public class BridgeService extends AccessibilityService {
    public static volatile BridgeService instance;
    private volatile boolean running=false;
    private volatile String message="중지됨",pairCode="";
    private volatile long epoch=0;
    private final Handler main=new Handler(Looper.getMainLooper());
    private Thread worker;
    private String token;
    private SharedPreferences prefs;
    private String lastHash="";
    private long lastRead=0;

    protected void onServiceConnected(){
        instance=this;prefs=getSharedPreferences("bridge",MODE_PRIVATE);
        token=prefs.getString("token","");
        if(token.isEmpty()){
            byte[] random=new byte[32];new SecureRandom().nextBytes(random);token=hex(random);
            if(!prefs.edit().putString("token",token).commit()){message="인증정보 저장 실패";return;}
        }
        pairCode=sha(token).substring(0,8).toUpperCase();
        message="중지됨 — 실행은 직접 시작하세요.";
    }
    public String status(){return "접근성 연결됨\n페어링 코드: "+pairCode+"\n"+message;}
    public synchronized void startDiagnostics(){
        if(running||worker!=null&&worker.isAlive())return;
        running=true;long run=++epoch;message="로컬 서버 연결 중";
        worker=new Thread(()->loop(run),"BunjangBridge");worker.start();
    }
    public synchronized void stopDiagnostics(){running=false;epoch++;message="사용자가 중지함";if(worker!=null)worker.interrupt();}
    public void onInterrupt(){stopDiagnostics();}
    public void onDestroy(){stopDiagnostics();instance=null;super.onDestroy();}
    public void onAccessibilityEvent(AccessibilityEvent event){} // Never collect notification text.
    private boolean active(long run){return running&&run==epoch;}
    private void loop(long run){
        int failures=0;
        while(active(run))try{
            JSONObject request=new JSONObject().put("version",1).put("device","note9")
                .put("app_version",3).put("preflight_protocol",1)
                .put("max_tap_points",Math.min(6,GestureDescription.getMaxStrokeCount()));
            JSONObject reply=post("/v1/poll",request);
            if(!active(run))break;
            if(reply.optBoolean("pairing")){message="서버 승인 대기 — 코드 "+pairCode;Thread.sleep(2000);continue;}
            if(!reply.optBoolean("ok"))throw new Exception(reply.optString("error","server_error"));
            JSONObject cmd=reply.getJSONObject("command");
            JSONObject result=execute(cmd,run);
            if(!active(run))break;
            // Reporting may repeat; executing this command does not.
            JSONObject report=new JSONObject().put("version",1).put("id",cmd.getString("id")).put("result",result);
            for(int attempt=0;;attempt++){
                if(!active(run))break;
                try{JSONObject ack=post("/v1/result",report);if(!ack.optBoolean("ok"))throw new Exception("report_rejected");break;}
                catch(Exception e){if(attempt>=3)throw e;Thread.sleep(500L*(attempt+1));}
            }
            failures=0;message="서버: "+reply.optString("mode","observe")+" / "+result.optString("status","unknown")+
                "\n"+reply.optString("message","");
            Thread.sleep(kindReadyRead(cmd,result)?100:800);
        }catch(InterruptedException e){break;}
        catch(Exception e){message="연결 재시도: "+e.getClass().getSimpleName();failures=Math.min(failures+1,6);
            try{Thread.sleep(Math.min(15000,500L*(1<<failures)));}catch(InterruptedException stop){break;}}
        if(run==epoch)running=false;
    }
    private JSONObject execute(JSONObject cmd,long run)throws Exception{
        String id=cmd.getString("id"),kind=cmd.getString("type");
        if(!id.matches("[a-f0-9]{32}"))throw new Exception("invalid_command_id");
        String commandHash=sha(cmd.toString()),saved=prefs.getString("command."+id,"");
        if(!saved.isEmpty()){
            JSONObject entry=new JSONObject(saved);
            if(!entry.getString("hash").equals(commandHash))throw new Exception("command_identity_conflict");
            return entry.has("result")?entry.getJSONObject("result"):new JSONObject().put("status","uncertain");
        }
        CountDownLatch latch=new CountDownLatch(1);JSONObject[] output=new JSONObject[1];
        AtomicReference<JSONObject> completed=new AtomicReference<>();
        AtomicReference<JSONObject> dispatchEvidence=new AtomicReference<>();
        main.post(()->{
            try{
                if(completed.get()!=null)return;
                if(!active(run)){output[0]=result("stopped");return;}
                long expires=cmd.optLong("expires",0),now=System.currentTimeMillis();
                boolean renewed=!kind.equals("read")&&cmd.optInt("preflight_protocol",0)==1;
                if(expires<now||expires>now+15000){output[0]=renewed?deferred("deadline_elapsed"):result("expired");return;}
                JSONObject frame=readScreen();
                if(completed.get()!=null||!active(run)){output[0]=result("stopped");return;}
                if(kind.equals("read")){output[0]=frame;return;}
                // No arbitrary intent, shell, Javascript, text input or app switching.
                if(!kind.equals("tap")&&!kind.equals("refresh")&&!kind.equals("back")){output[0]=result("unsupported");return;}
                String frameStatus=frame.optString("status");
                if(!frameStatus.equals("ready")){
                    String reason=frameStatus.equals("locked")?"device_locked":frameStatus.equals("other_app")?"other_app":
                        frameStatus.equals("read_slow")?"screen_slow":"empty";
                    output[0]=renewed?deferred(reason):result("screen_changed");return;
                }
                if(!frame.optString("hash").equals(cmd.optString("screen_hash"))){
                    output[0]=renewed?deferred("frame_changed"):result("screen_changed");return;
                }
                final JSONObject proof=renewed?new JSONObject().put("version",1).put("read_at",frame.getLong("read_at"))
                    .put("read_finished_at",frame.getLong("read_finished_at")).put("hash",frame.getString("hash")):null;
                GestureDescription.Builder builder=new GestureDescription.Builder();
                if(kind.equals("refresh")){
                    Path p=new Path();p.moveTo(200,350);p.lineTo(200,1200);
                    builder.addStroke(new GestureDescription.StrokeDescription(p,0,500));
                }else if(kind.equals("tap")){
                    JSONArray points=cmd.getJSONArray("points");
                    if(points.length()<1||points.length()>Math.min(6,GestureDescription.getMaxStrokeCount()))throw new Exception("invalid_points");
                    for(int i=0;i<points.length();i++){
                        JSONObject point=points.getJSONObject(i);int x=point.getInt("x"),y=point.getInt("y");
                        if(x<=249||x>=840||y<=320||y>=1944)throw new Exception("invalid_coordinates");
                        for(int j=0;j<i;j++)if(Math.abs(y-points.getJSONObject(j).getInt("y"))<100)throw new Exception("overlapping_points");
                        Path p=new Path();p.moveTo(x,y);builder.addStroke(new GestureDescription.StrokeDescription(p,0,120));
                    }
                }
                final GestureDescription gesture=kind.equals("back")?null:builder.build();
                // Persist reservation BEFORE dispatch. Process death never retries it.
                JSONObject entry=new JSONObject().put("hash",commandHash);
                if(!prefs.edit().putString("command."+id,entry.toString()).commit()){output[0]=result("journal_failed");return;}
                pruneJournal(id);
                long started=System.currentTimeMillis();
                if(completed.get()!=null||!active(run)){output[0]=result("stopped");return;}
                if(!PreflightTiming.canStart(frame.getLong("read_at"),frame.getLong("read_finished_at"),started,expires)){
                    output[0]=renewed?deferred(started>expires?"deadline_elapsed":"screen_slow"):result("stopped");return;
                }
                JSONObject evidence=new JSONObject().put("started",started);
                if(proof!=null)evidence.put("preflight",proof);
                dispatchEvidence.set(evidence);
                if(kind.equals("back")){
                    boolean accepted=performGlobalAction(GLOBAL_ACTION_BACK);
                    output[0]=withEvidence(result(accepted?"returned":"cancelled").put("returned",System.currentTimeMillis()),dispatchEvidence.get());return;
                }
                boolean accepted=dispatchGesture(gesture,new GestureResultCallback(){
                    private void finish(String status){try{completed.compareAndSet(null,withEvidence(result(status).put("returned",System.currentTimeMillis()),dispatchEvidence.get()));}catch(Exception ignored){completed.compareAndSet(null,result("uncertain"));}latch.countDown();}
                    public void onCompleted(GestureDescription g){finish("returned");}
                    public void onCancelled(GestureDescription g){finish("cancelled");}
                },main);
                if(accepted)return;
                output[0]=withEvidence(result("cancelled").put("returned",System.currentTimeMillis()),dispatchEvidence.get());
            }catch(Exception e){output[0]=withEvidence(result("uncertain"),dispatchEvidence.get());}
            finally{if(output[0]!=null){completed.compareAndSet(null,output[0]);latch.countDown();}}
        });
        if(!latch.await(12,TimeUnit.SECONDS))completed.compareAndSet(null,withEvidence(result("uncertain"),dispatchEvidence.get()));
        completed.compareAndSet(null,withEvidence(result("uncertain"),dispatchEvidence.get()));
        JSONObject outcome=completed.get();
        if(!kind.equals("read")){
            JSONObject entry=new JSONObject().put("hash",commandHash).put("result",outcome);
            if(!prefs.edit().putString("command."+id,entry.toString()).commit())return result("uncertain");
            pruneJournal(id);
        }
        return outcome;
    }
    private void pruneJournal(String keep){
        // At most one in-flight command; retain recent identities for process recovery.
        String[] ids=prefs.getString("journal","").split(",");StringBuilder list=new StringBuilder();
        SharedPreferences.Editor edit=prefs.edit();
        for(int i=0;i<ids.length;i++)if(!ids[i].isEmpty()&&!ids[i].equals(keep)){
            if(i<ids.length-31)edit.remove("command."+ids[i]);else list.append(ids[i]).append(',');
        }
        edit.putString("journal",list.append(keep).toString()).commit();
    }
    private JSONObject readScreen()throws Exception{
        long readStarted=System.currentTimeMillis();
        PowerManager power=(PowerManager)getSystemService(POWER_SERVICE);
        KeyguardManager lock=(KeyguardManager)getSystemService(KEYGUARD_SERVICE);
        if(!power.isInteractive()||lock.isKeyguardLocked())return result("locked");
        AccessibilityNodeInfo root=getRootInActiveWindow();
        if(root==null)return result("empty");
        if(!"kr.co.quicket".contentEquals(root.getPackageName()==null?"":root.getPackageName())){root.recycle();return result("other_app");}
        JSONObject screen=new JSONObject();ArrayDeque<AccessibilityNodeInfo> queue=new ArrayDeque<>();queue.add(root);int n=0;
        while(!queue.isEmpty()&&n<1600){
            AccessibilityNodeInfo node=queue.removeFirst();int index=n++;
            for(int i=0;i<node.getChildCount();i++){AccessibilityNodeInfo child=node.getChild(i);if(child!=null)queue.add(child);}
            Rect b=new Rect();node.getBoundsInScreen(b);
            if(node.isVisibleToUser()&&b.centerX()>0&&b.centerX()<1080&&b.centerY()>55&&b.centerY()<1950&&
                (node.getPackageName()==null||"kr.co.quicket".contentEquals(node.getPackageName()))){
                String name=node.getViewIdResourceName();if(name==null)name="index:"+index;
                if(screen.has(name))name=name+"$"+index;
                CharSequence text=node.getText();if(text==null)text=node.getContentDescription();
                String value=text==null?"":text.toString();if(value.length()>240)value=value.substring(0,240);
                screen.put(name,new JSONObject().put("text",value).put("centerX",b.centerX()).put("centerY",b.centerY())
                    .put("startX",b.left).put("startY",b.top).put("endX",b.right).put("endY",b.bottom)
                    .put("clickable",node.isClickable()).put("visibleToUser",true)
                    .put("className",node.getClassName()==null?"":node.getClassName().toString()));
            }node.recycle();
        }
        while(!queue.isEmpty())queue.removeFirst().recycle();
        if(System.currentTimeMillis()-readStarted>6000)return result("read_slow");
        lastRead=readStarted;lastHash=sha(screen.toString());
        return result("ready").put("screen",screen).put("hash",lastHash).put("read_at",lastRead)
            .put("read_finished_at",System.currentTimeMillis());
    }
    private JSONObject post(String path,JSONObject body)throws Exception{
        HttpURLConnection c=(HttpURLConnection)new URL("http://127.0.0.1:8792"+path).openConnection();
        try{
            c.setInstanceFollowRedirects(false);c.setRequestMethod("POST");c.setConnectTimeout(4000);c.setReadTimeout(6000);
            c.setRequestProperty("Content-Type","application/json");c.setRequestProperty("Authorization","Bearer "+token);c.setDoOutput(true);
            byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);c.setFixedLengthStreamingMode(bytes.length);
            try(java.io.OutputStream out=c.getOutputStream()){out.write(bytes);}
            if(c.getResponseCode()!=200)throw new Exception("http_"+c.getResponseCode());
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()){
                byte[] buf=new byte[4096];int count;while((count=in.read(buf))!=-1){if(out.size()+count>262144)throw new Exception("reply_too_large");out.write(buf,0,count);}
                return new JSONObject(new String(out.toByteArray(),StandardCharsets.UTF_8));
            }
        }finally{c.disconnect();}
    }
    private static JSONObject result(String value){JSONObject o=new JSONObject();try{o.put("status",value);}catch(Exception ignored){}return o;}
    private static JSONObject deferred(String reason){JSONObject o=result("deferred");try{o.put("reason",reason);}catch(Exception ignored){}return o;}
    private static boolean kindReadyRead(JSONObject command,JSONObject outcome){return command.optString("type").equals("read")&&outcome.optString("status").equals("ready");}
    private static JSONObject withEvidence(JSONObject out,JSONObject evidence){
        try{if(evidence!=null){out.put("started",evidence.getLong("started"));if(evidence.has("preflight"))out.put("preflight",evidence.getJSONObject("preflight"));}}
        catch(Exception ignored){}return out;
    }
    private static String sha(String s){try{return hex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);}}
    private static String hex(byte[] bytes){StringBuilder s=new StringBuilder();for(byte b:bytes)s.append(String.format("%02x",b&255));return s.toString();}
}
