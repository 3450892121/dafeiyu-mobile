package app.dafeiyu.mobile;

import android.content.Context;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

final class ControlClient {
    final NativeRuntime runtime;
    final String token;
    ControlClient(Context context)throws Exception{
        runtime=new NativeRuntime(context);
        token=new JSONObject(new String(Files.readAllBytes(new File(runtime.home,"data/app-control.json").toPath()),StandardCharsets.UTF_8)).getString("token");
    }
    String root()throws JSONException{return "http://127.0.0.1:"+runtime.layout.getJSONObject("ports").getInt("control");}
    JSONObject get(String route)throws Exception{return request(route,null);}
    JSONObject post(String route,JSONObject fields)throws Exception{return request(route,fields);}
    private JSONObject request(String route,JSONObject fields)throws Exception{
        if(!route.matches("/api/[a-z]+"))throw new IOException("无效控制请求");
        HttpURLConnection connection=(HttpURLConnection)new URL(root()+route).openConnection();
        connection.setConnectTimeout(3000);connection.setReadTimeout(route.equals("/api/backup")?180000:45000);
        connection.setInstanceFollowRedirects(false);connection.setRequestProperty("Authorization","Bearer "+token);
        try{
            if(fields!=null){connection.setRequestMethod("POST");connection.setDoOutput(true);connection.setRequestProperty("Content-Type","application/json; charset=utf-8");try(OutputStream out=connection.getOutputStream()){out.write(fields.toString().getBytes(StandardCharsets.UTF_8));}}
            int code=connection.getResponseCode();InputStream source=code>=400?connection.getErrorStream():connection.getInputStream();
            if(source==null)throw new IOException("本机管理服务未返回数据");
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();try(InputStream in=source){byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1){if(bytes.size()+n>8*1024*1024)throw new IOException("响应过大");bytes.write(b,0,n);}}
            JSONObject result=new JSONObject(bytes.toString("UTF-8"));
            if(code!=200||!result.optBoolean("ok",true))throw new IOException(result.optString("error","操作未完成，请查看日志"));
            return result;
        }finally{connection.disconnect();}
    }
}
