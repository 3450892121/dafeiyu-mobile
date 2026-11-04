package app.dafeiyu.mobile;

import android.app.*;
import android.content.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.text.InputType;
import android.text.format.Formatter;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends Activity {
    private Ui ui;
    private Catalogue catalogue;
    private LinearLayout content;
    private TextView stateText;
    private final Button[] tabs=new Button[3];
    private final Handler timer=new Handler(Looper.getMainLooper());
    private final ExecutorService work=Executors.newSingleThreadExecutor();
    private int selected=0;
    private String backupPassword="";
    private File selectedQr;
    private final Runnable refresh=new Runnable(){public void run(){if(stateText!=null)stateText.setText(EngineService.alive()?EngineService.message():"本机服务尚未启动；启动后读取实时状态");timer.postDelayed(this,1000);}};

    @Override public void onCreate(Bundle state){
        super.onCreate(state);ui=new Ui(this);
        try{catalogue=new Catalogue(this);}catch(Exception ignored){}
        if(state!=null)selected=state.getInt("selected",0);
        LinearLayout root=ui.column();root.setBackgroundColor(ui.color(R.color.background));root.setFitsSystemWindows(true);
        int pad=ui.dim(R.dimen.page_padding);
        LinearLayout header=new LinearLayout(this);header.setGravity(Gravity.CENTER_VERTICAL);header.setPadding(pad,pad,pad,ui.dim(R.dimen.space_small));
        ImageView fish=new ImageView(this);fish.setImageResource(R.drawable.ic_fish);header.addView(fish,new LinearLayout.LayoutParams(ui.dim(R.dimen.icon_size),ui.dim(R.dimen.icon_size)));
        LinearLayout title=ui.column();title.setPadding(ui.dim(R.dimen.space_medium),0,0,0);
        title.addView(ui.text(getString(R.string.app_name),R.dimen.title_text,R.color.ink,true));title.addView(ui.text("从新的相遇开始",R.dimen.caption_text,R.color.muted,false));header.addView(title);root.addView(header);
        LinearLayout nav=new LinearLayout(this);nav.setPadding(pad,0,pad,0);String[] labels={"概览","功能","本机检查"};
        for(int i=0;i<3;i++){final int tab=i;tabs[i]=ui.button(labels[i],()->showPage(tab));nav.addView(tabs[i],new LinearLayout.LayoutParams(0,-2,1));}root.addView(nav);
        ScrollView scroll=new ScrollView(this);content=ui.column();content.setPadding(pad,0,pad,pad);scroll.addView(content);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);showPage(selected);
    }
    @Override public void onSaveInstanceState(Bundle state){state.putInt("selected",selected);super.onSaveInstanceState(state);}
    @Override protected void onResume(){super.onResume();timer.post(refresh);}
    @Override protected void onPause(){timer.removeCallbacks(refresh);super.onPause();}
    @Override protected void onDestroy(){work.shutdownNow();super.onDestroy();}

    private void showPage(int index){
        selected=index;content.removeAllViews();stateText=null;
        for(int i=0;i<3;i++)tabs[i].setTextColor(ui.color(i==selected?R.color.accent:R.color.muted));
        if(index==1)showFeatures();else if(index==2)showDevice();else showOverview();
    }
    private void showOverview(){
        LinearLayout state=ui.card(content,"手机独立运行","",false);
        stateText=ui.text(EngineService.alive()?EngineService.message():"本机服务尚未启动",R.dimen.body_text,R.color.ink,false);ui.add(state,stateText,R.dimen.space_small);
        ui.add(state,ui.button("初始化 / 修复运行组件",()->{
            if(EngineService.busy()){toast("运行任务正在进行，请等待当前步骤完成");return;}
            new AlertDialog.Builder(this).setTitle("准备本机运行环境").setMessage("本包已内置 Ubuntu、Python 依赖、NapCat 和 CPU 识别模型，准备这些组件无需访问 GitHub、Ubuntu 或 PyPI。QQ 客户端因无公开再分发授权，首次仍从腾讯官方 HTTPS 地址下载约 200 MB。请至少预留 4 GB 空间并连接电源。所有组件仅安装在本软件内部；AI 接口和 QQ 本人登录仍需联网。")
                .setNegativeButton("返回",null).setPositiveButton("开始准备",(d,w)->service(EngineService.INITIALIZE)).show();
        }),R.dimen.space_small);
        ui.add(state,ui.button("启动机器人",()->service(EngineService.RUN)),R.dimen.space_small);
        ui.add(state,ui.button("设置与管理",()->openPanel("home")),R.dimen.space_small);
        ui.add(state,ui.button("QQ 登录",()->openPanel("qq")),R.dimen.space_small);
        ui.add(state,ui.button("保存登录二维码",this::saveQr),R.dimen.space_small);
        ui.add(state,ui.button("停止运行",()->service(EngineService.STOP)),R.dimen.space_small);
        LinearLayout features=ui.card(content,"完整功能管理","AI 接口、人设、记忆、成长、梦、日记、隐私、好友群、表情包、提醒、图片、视频、文件、B站与空间设置。",false);
        ui.add(features,ui.button("打开完整管理页",()->openPanel("panel")),R.dimen.space_small);
        LinearLayout data=ui.card(content,"本实例资料","新的小号、新的聊天和记忆。加密备份包含本实例资料与接口配置，请保管备份密码。",false);
        ui.add(data,ui.button("导出加密备份",()->password(false)),R.dimen.space_small);
        ui.add(data,ui.button("导入加密备份",()->password(true)),R.dimen.space_small);
        ui.add(data,ui.button("查看运行日志",this::showLog),R.dimen.space_small);
        ui.card(content,"等待手机实测","代码检查与电脑隔离测试不代表 QQ 已登录。手机内核兼容、实际收发和熄屏运行需要在你的手机上验证。",true);
    }
    private void showFeatures(){
        ui.card(content,"功能复刻清单","“已实现 · 待手机验收”表示已有功能源码和隔离证据，尚未证明手机实测通过。",false);
        if(catalogue==null){ui.card(content,"清单无法读取","请重新安装完整安装包",true);return;}
        for(Catalogue.Feature feature:catalogue.features)ui.card(content,feature.name,feature.status+" · "+(feature.optional?"可选能力":"完整复刻目标")+(feature.detail.isEmpty()?"":"\n"+feature.detail),false);
    }
    private void showDevice(){
        LinearLayout card=ui.card(content,"本机条件","",false);
        detail(card,"安卓 "+Build.VERSION.RELEASE+"（"+Build.VERSION.SDK_INT+"）");detail(card,"处理器："+String.join(" / ",Build.SUPPORTED_ABIS));
        ActivityManager.MemoryInfo info=new ActivityManager.MemoryInfo();((ActivityManager)getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(info);
        detail(card,"可用内存 "+Formatter.formatFileSize(this,info.availMem)+" / "+Formatter.formatFileSize(this,info.totalMem));
        detail(card,"应用存储可用 "+Formatter.formatFileSize(this,new StatFs(getFilesDir().getPath()).getAvailableBytes()));
        detail(card,((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).areNotificationsEnabled()?"运行通知已允许":"运行通知未允许，可在通知设置中开启");
        detail(card,"建议 64 位 ARM、至少 6 GB 内存及 6 GB 空闲存储；初始化仍以实时空间检查及设备实测为准。");
        ui.add(card,ui.button("后台与电池设置",()->{
            try{startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));}catch(Exception e){startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));}
        }),R.dimen.space_small);
        ui.add(card,ui.button("通知设置",()->startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName()))),R.dimen.space_small);
        ui.add(card,ui.button("重新检查",()->showPage(2)),R.dimen.space_small);
        ui.add(card,ui.button("关于与第三方许可",()->work.execute(()->{try{
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();try(InputStream in=getAssets().open("licenses.txt")){byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1)bytes.write(b,0,n);}
            String text=bytes.toString("UTF-8");runOnUiThread(()->{ScrollView scroll=new ScrollView(this);TextView body=new TextView(this);body.setText(text);body.setTextIsSelectable(true);scroll.addView(body);new AlertDialog.Builder(this).setTitle("第三方许可").setView(scroll).setPositiveButton("关闭",null).show();});
        }catch(Exception e){error(e);}})),R.dimen.space_small);
        ui.card(content,"后台运行","运行时保留通知。请允许本软件后台运行，并检查系统的自启动与电池限制。手动停止后不会自动启动。",false);
    }
    private void service(String action){
        try{EngineService.send(this,action);toast(action.equals(EngineService.STOP)?"正在停止":"已交给本机后台服务处理");}catch(Exception e){error(e);}
    }
    private void openPanel(String section){
        if(EngineService.busy()){toast("正在准备运行组件，请稍后打开管理页");return;}
        try{if(!new NativeRuntime(this).ready()){toast("请先初始化运行组件");return;}EngineService.send(this,EngineService.OPEN);}catch(Exception e){error(e);return;}
        work.execute(()->{try{
            for(int i=0;i<60;i++){
                try{new ControlClient(this).get("/api/basic");runOnUiThread(()->startActivity(new Intent(this,PanelActivity.class).putExtra("section",section)));return;}
                catch(Exception e){Thread.sleep(500);}
            }throw new IOException("管理服务尚未打开，请查看运行日志");
        }catch(Exception e){error(e);}});
    }
    private void password(boolean importing){
        if(!EngineService.alive()||EngineService.busy()){toast("请先打开设置与管理，等待本机服务就绪");return;}
        EditText password=new EditText(this);password.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);password.setHint("至少 8 个字符");
        new AlertDialog.Builder(this).setTitle(importing?"输入备份密码":"设置备份密码").setView(password).setNegativeButton("取消",null)
            .setPositiveButton("继续",(d,w)->{
                String entered=password.getText().toString();password.setText("");if(entered.length()<8){toast("密码至少需要 8 个字符");return;}
                if(importing){backupPassword=entered;startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("application/octet-stream").addCategory(Intent.CATEGORY_OPENABLE),82);}
                else work.execute(()->{try{
                    new ControlClient(this).post("/api/backup",new JSONObject().put("action","export").put("password",entered));
                    runOnUiThread(()->startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT).setType("application/octet-stream").addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_TITLE,"大肥鱼-"+new java.text.SimpleDateFormat("yyyyMMdd-HHmm",Locale.ROOT).format(new Date())+".dafm"),81));
                }catch(Exception e){error(e);}});
            }).show();
    }
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);
        if(request==83){
            final File qr=selectedQr;selectedQr=null;
            if(result==RESULT_OK&&data!=null&&data.getData()!=null&&qr!=null){final Uri target=data.getData();work.execute(()->{try(InputStream in=new FileInputStream(qr);OutputStream out=getContentResolver().openOutputStream(target,"wt")){if(out==null)throw new IOException("无法保存二维码");copyBounded(in,out);toast("二维码已保存，请在对应 App 中从相册识别");}catch(Exception e){error(e);}});}return;
        }
        if(request!=81&&request!=82)return;
        final String pass=backupPassword;backupPassword="";
        if(result!=RESULT_OK||data==null||data.getData()==null)return;final Uri uri=data.getData();
        work.execute(()->{try{
            NativeRuntime runtime=new NativeRuntime(this);
            if(request==81){try(InputStream in=new FileInputStream(runtime.transfer());OutputStream out=getContentResolver().openOutputStream(uri,"wt")){if(out==null)throw new IOException("无法写入所选文件");copyBounded(in,out);}toast("加密备份已导出");}
            else{
                File temporary=new File(runtime.transfer().getPath()+".part");
                try(InputStream in=getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(temporary)){if(in==null)throw new IOException("无法读取备份");copyBounded(in,out);}
                Files.move(temporary.toPath(),runtime.transfer().toPath(),StandardCopyOption.REPLACE_EXISTING);
                new ControlClient(this).post("/api/backup",new JSONObject().put("action","import").put("password",pass));
                getSharedPreferences("runtime",0).edit().putBoolean("keep_running",false).apply();toast("资料已恢复，已暂停机器人，请核对设置后手动启动");
            }
            Files.deleteIfExists(runtime.transfer().toPath());
        }catch(Exception e){error(e);}});
    }
    private static void copyBounded(InputStream in,OutputStream out)throws IOException{byte[] buffer=new byte[65536];int n;long total=0;while((n=in.read(buffer))!=-1){total+=n;if(total>512L*1024*1024)throw new IOException("文件超过 512 MB 上限");out.write(buffer,0,n);}}
    private void saveQr(){new AlertDialog.Builder(this).setTitle("选择登录二维码").setItems(new String[]{"QQ 小号","B站"},(d,which)->{
        try{NativeRuntime runtime=new NativeRuntime(this);selectedQr=new File(runtime.home,which==0?"Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png":"bili/login_qrcode.png");
            if(!selectedQr.isFile()||selectedQr.length()>1024*1024){selectedQr=null;toast("请先在对应登录页生成二维码");return;}
            startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT).setType("image/png").addCategory(Intent.CATEGORY_OPENABLE).putExtra(Intent.EXTRA_TITLE,which==0?"大肥鱼-QQ登录.png":"大肥鱼-B站登录.png"),83);
        }catch(Exception e){error(e);}
    }).show();}
    private void showLog(){work.execute(()->{try{
        File file=new NativeRuntime(this).logFile();String value="暂无运行日志";
        if(file.isFile())try(RandomAccessFile log=new RandomAccessFile(file,"r")){log.seek(Math.max(0,log.length()-60000));byte[] bytes=new byte[(int)Math.min(60000,log.length())];log.readFully(bytes);value=new String(bytes,java.nio.charset.StandardCharsets.UTF_8);}
        final String text=value;runOnUiThread(()->{ScrollView scroll=new ScrollView(this);TextView body=new TextView(this);body.setText(text);body.setTextIsSelectable(true);scroll.addView(body);new AlertDialog.Builder(this).setTitle("本机运行日志").setView(scroll).setPositiveButton("关闭",null).show();});
    }catch(Exception e){error(e);}});}
    private void detail(LinearLayout card,String value){ui.add(card,ui.text(value,R.dimen.body_text,R.color.ink,false),R.dimen.space_medium);}
    private void toast(String message){runOnUiThread(()->Toast.makeText(this,message,Toast.LENGTH_LONG).show());}
    private void error(Exception error){toast(error.getMessage()==null?"操作未完成，请查看日志":error.getMessage());}
}
