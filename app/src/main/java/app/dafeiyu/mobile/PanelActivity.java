package app.dafeiyu.mobile;

import android.app.*;
import android.content.*;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.*;
import android.widget.*;
import org.json.*;
import java.net.URI;
import java.util.*;

/** Web UI for the app's loopback services. No JavaScript-to-native bridge or file access. */
public final class PanelActivity extends Activity {
    private WebView web;
    private ValueCallback<Uri[]> chooser;
    private Set<Integer> ports=new HashSet<>();
    private Set<String> documents=Collections.synchronizedSet(new HashSet<>());
    @Override public void onCreate(Bundle state){
        super.onCreate(state);setTitle("大肥鱼 · 管理");
        LinearLayout page=new LinearLayout(this);page.setOrientation(LinearLayout.VERTICAL);
        Button back=new Button(this);back.setText("返回应用");back.setOnClickListener(v->finish());page.addView(back);
        web=new WebView(this);page.addView(web,new LinearLayout.LayoutParams(-1,0,1));setContentView(page);
        WebSettings settings=web.getSettings();settings.setJavaScriptEnabled(true);settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);settings.setAllowContentAccess(true);settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){
                if(allowed(request.getUrl().toString()))return false;
                if(request.isForMainFrame())Toast.makeText(PanelActivity.this,"此页面仅打开本软件的本机管理服务",Toast.LENGTH_LONG).show();
                return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                String scheme=request.getUrl().getScheme();
                if(allowed(request.getUrl().toString())||"data".equals(scheme)||"blob".equals(scheme)||documents.contains(request.getUrl().toString()))return null;
                return new WebResourceResponse("text/plain","UTF-8",new java.io.ByteArrayInputStream(new byte[0]));
            }
        });
        web.setWebChromeClient(new WebChromeClient(){
            @Override public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params){
                if(chooser!=null)chooser.onReceiveValue(null);chooser=callback;
                Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,params.getMode()==FileChooserParams.MODE_OPEN_MULTIPLE);
                startActivityForResult(intent,71);return true;
            }
        });
        new Thread(()->{
            try{
                ControlClient client=new ControlClient(this);JSONObject p=client.runtime.layout.getJSONObject("ports");
                synchronized(ports){ports.add(p.getInt("control"));ports.add(p.getInt("panel"));ports.add(p.getInt("qqWebui"));}
                String section=getIntent().getStringExtra("section"),url=client.root()+"/";
                Map<String,String> headers=new HashMap<>();headers.put("Authorization","Bearer "+client.token);
                if("panel".equals(section)||"qq".equals(section)){
                    url=client.get("/api/urls").optString(section);headers.clear();
                    if(url.isEmpty())throw new Exception("请先启动 QQ 组件，再打开登录页");
                }
                if(!allowed(url))throw new Exception("管理地址不属于本机运行层");
                final String location=url;runOnUiThread(()->web.loadUrl(location,headers));
            }catch(Exception error){runOnUiThread(()->new AlertDialog.Builder(this).setMessage(error.getMessage()).setPositiveButton("返回",(d,w)->finish()).show());}
        },"mobile-panel-load").start();
    }
    private boolean allowed(String value){try{URI uri=new URI(value);synchronized(ports){return "http".equals(uri.getScheme())&&"127.0.0.1".equals(uri.getHost())&&uri.getUserInfo()==null&&ports.contains(uri.getPort());}}catch(Exception e){return false;}}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);
        if(request==71&&chooser!=null){ArrayList<Uri> uris=new ArrayList<>();if(result==RESULT_OK&&data!=null){if(data.getClipData()!=null){for(int i=0;i<data.getClipData().getItemCount();i++)uris.add(data.getClipData().getItemAt(i).getUri());}else if(data.getData()!=null)uris.add(data.getData());}
            for(Uri uri:uris)documents.add(uri.toString());chooser.onReceiveValue(uris.isEmpty()?null:uris.toArray(new Uri[0]));chooser=null;}}
    @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override protected void onDestroy(){if(chooser!=null)chooser.onReceiveValue(null);if(web!=null){web.stopLoading();web.destroy();}super.onDestroy();}
}
