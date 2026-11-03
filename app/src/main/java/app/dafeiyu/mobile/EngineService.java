package app.dafeiyu.mobile;

import android.app.*;
import android.content.*;
import android.net.*;
import android.os.*;
import org.json.*;
import java.io.*;
import java.util.concurrent.*;

public final class EngineService extends Service {
    public static final String INITIALIZE="initialize", RUN="run", OPEN="open", STOP="stop";
    private static volatile String message="尚未启动";
    private static volatile boolean busy=false, alive=false;
    private final ExecutorService work=Executors.newSingleThreadExecutor();
    private final Handler timer=new Handler(Looper.getMainLooper());
    private NativeRuntime runtime;
    private PowerManager.WakeLock wake;
    private ConnectivityManager.NetworkCallback network;
    private volatile boolean shuttingDown=false;
    private volatile Future<?> activeTask;
    public static String message(){return message;}
    public static boolean busy(){return busy;}
    public static boolean alive(){return alive;}

    public static void send(Context context,String action){
        Intent intent=new Intent(context,EngineService.class).setAction(action);
        if(Build.VERSION.SDK_INT>=26)context.startForegroundService(intent);else context.startService(intent);
    }

    @Override public void onCreate(){
        super.onCreate();alive=true;
        NotificationManager notifications=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        notifications.createNotificationChannel(new NotificationChannel("runtime","机器人运行",NotificationManager.IMPORTANCE_LOW));
        startForeground(1,notification("本机运行服务已打开"));
        try{
            runtime=new NativeRuntime(this);
            PowerManager power=(PowerManager)getSystemService(POWER_SERVICE);
            wake=power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,getPackageName()+":runtime");
            wake.setReferenceCounted(false);
            ConnectivityManager connectivity=(ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
            network=new ConnectivityManager.NetworkCallback(){@Override public void onAvailable(Network n){
                try{if(!shuttingDown)work.execute(()->{try{if(runtime.ready())runtime.refreshDns();}catch(Exception ignored){}});}catch(RejectedExecutionException ignored){}
            }};
            connectivity.registerDefaultNetworkCallback(network);
        }catch(Exception error){update("运行层准备失败："+error.getMessage());}
        timer.postDelayed(monitor,30000);
    }

    @Override public int onStartCommand(Intent intent,int flags,int id){
        String action=intent==null?OPEN:intent.getAction();
        if(STOP.equals(action)){
            getSharedPreferences("runtime",0).edit().putBoolean("keep_running",false).apply();
            if(busy&&activeTask!=null)activeTask.cancel(true);
            work.execute(()->{
                busy=true;update("正在停止本实例……");
                try{new ControlClient(this).post("/api/action",new JSONObject().put("command","stop_all"));}catch(Exception ignored){}
                if(runtime!=null)runtime.stop();busy=false;update("已停止");stopSelf();
            });
            return START_NOT_STICKY;
        }
        if(busy)return START_NOT_STICKY;
        busy=true;
        activeTask=work.submit(()->{
            try{
                if(runtime==null)throw new IOException("运行环境无法打开");
                acquireWake();
                if(INITIALIZE.equals(action))runtime.initialize(this::update);
                update("正在打开本机管理服务……");runtime.start(this::update);
                ControlClient client=awaitControl();
                if(RUN.equals(action)){
                    getSharedPreferences("runtime",0).edit().putBoolean("keep_running",true).apply();
                    client.post("/api/action",new JSONObject().put("command","start_all"));
                }
                render(client.get("/api/status"));
            }catch(Exception error){update("未完成："+safe(error.getMessage()));}
            finally{busy=false;}
        });
        return START_NOT_STICKY;
    }

    private ControlClient awaitControl()throws Exception{
        ControlClient client=new ControlClient(this);Exception last=null;
        for(int i=0;i<60&&!shuttingDown;i++){
            if(!runtime.running())throw new IOException("本机运行进程已退出，请查看初始化日志");
            try{client.get("/api/basic");return client;}catch(Exception e){last=e;Thread.sleep(500);}
        }
        throw new IOException("本机管理服务未就绪",last);
    }
    private void render(JSONObject state){
        if(!state.optBoolean("autostart")&&!state.optBoolean("brain_running")&&wake!=null&&wake.isHeld())wake.release();
        if(state.optBoolean("connection_blocked"))update("QQ 连接已暂停，请打开登录页检查后手动恢复");
        else if(state.optBoolean("qq_online")&&state.optBoolean("brain_running"))update("QQ 已连接 · 大脑正在本机运行");
        else if(state.optBoolean("qq_online"))update("QQ 已连接，请检查 AI 和管理员设置");
        else if(!state.optBoolean("small_account_configured")||!state.optBoolean("owner_configured")||!state.optBoolean("model_configured"))update("请在设置中填写新小号、管理员和 AI 接口");
        else update("尚未登录 QQ，请打开登录页完成登录");
    }
    private final Runnable monitor=new Runnable(){public void run(){
        if(shuttingDown)return;
        if(!busy&&runtime!=null&&runtime.running())work.execute(()->{
            try{JSONObject state=new ControlClient(EngineService.this).get("/api/status");if(state.optBoolean("autostart")||state.optBoolean("brain_running"))acquireWake();render(state);}catch(Exception error){update("本机服务未响应，请查看日志");}
        });
        else if(!busy&&runtime!=null&&runtime.ready()&&!runtime.running())update("运行进程已退出，请检查日志后手动启动");
        timer.postDelayed(this,30000);
    }};
    private void acquireWake(){if(wake!=null&&(!wake.isHeld()))wake.acquire(4*60*60*1000L);}
    private String safe(String s){return s==null?"请查看日志":s.replaceAll("(?i)(token|api_key|authorization)([\\s:=]+)[^\\s,;]+","$1$2[已隐藏]");}
    private void update(String value){if(shuttingDown)return;message=safe(value);getSharedPreferences("runtime",0).edit().putString("last_message",message).apply();
        ((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).notify(1,notification(message));}
    private Notification notification(String detail){
        int flags=PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE;
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),flags);
        PendingIntent stop=PendingIntent.getService(this,1,new Intent(this,EngineService.class).setAction(STOP),flags);
        return new Notification.Builder(this,"runtime").setSmallIcon(R.drawable.ic_fish).setContentTitle(getString(R.string.app_name))
            .setContentText(detail).setContentIntent(open).setOngoing(true).setCategory(Notification.CATEGORY_SERVICE)
            .addAction(new Notification.Action.Builder(null,"停止",stop).build()).build();
    }
    @Override public void onDestroy(){
        shuttingDown=true;alive=false;busy=false;message="本机服务已停止";timer.removeCallbacksAndMessages(null);
        if(network!=null)try{((ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE)).unregisterNetworkCallback(network);}catch(Exception ignored){}
        // Stop only the PRoot instance we own; --kill-on-exit covers its guest children.
        new Thread(()->{if(runtime!=null)runtime.stop();if(wake!=null&&wake.isHeld())wake.release();},"mobile-runtime-stop").start();
        work.shutdownNow();stopForeground(true);super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent){return null;}
}
