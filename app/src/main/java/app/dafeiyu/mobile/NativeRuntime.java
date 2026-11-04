package app.dafeiyu.mobile;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.os.Build;
import android.os.StatFs;
import org.json.*;
import java.io.*;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.*;
import java.util.*;

/** The only Linux process launcher. All writable mounts belong to this application. */
final class NativeRuntime {
    interface Progress { void update(String message); }
    final Context context;
    final File engine, rootfs, payload, offline, home, scratch;
    final JSONObject layout;
    private volatile Process process;

    NativeRuntime(Context context) throws Exception {
        this.context=context.getApplicationContext();
        engine=new File(context.getFilesDir(),"engine"); rootfs=new File(engine,"rootfs");
        payload=new File(engine,"payload"); offline=new File(engine,"offline"); home=new File(context.getFilesDir(),"instance");
        scratch=new File(context.getCacheDir(),"linux");
        layout=new JSONObject(assetText("runtime/layout.json"));
    }

    boolean ready() { return new File(home,"data/runtime-ready.json").isFile() && new File(rootfs,"usr/bin/python3").exists(); }
    File transfer() { return new File(home,"backups/transfer.dafm"); }
    File logFile() { return new File(home,"data/runtime.log"); }

    void initialize(Progress progress) throws Exception {
        if(running())throw new IOException("请先停止运行再修复组件");
        if(!Arrays.asList(Build.SUPPORTED_ABIS).contains("arm64-v8a"))throw new IOException("当前安装包需要 64 位 ARM 安卓手机");
        JSONObject bundled=offlineManifest();
        long required=Math.max(4L*1024*1024*1024,offlineBytes(bundled)+3L*1024*1024*1024);
        if(new StatFs(context.getFilesDir().getPath()).getAvailableBytes()<required && !ready())
            throw new IOException("本机空间不足，首次准备需预留至少 "+((required+1024*1024-1)/(1024*1024))+" MB；内置组件还需要解压和安装空间");
        engine.mkdirs();new File(home,"data").mkdirs();new File(home,"backups").mkdirs();scratch.mkdirs();
        copyPayload();
        prepareOffline(bundled,progress);
        if(!new File(rootfs,".base-ready").isFile()) {
            progress.update("正在校验内置运行环境……");
            JSONObject lock=new JSONObject(assetText("runtime/components.lock.json"));
            String expected="";JSONArray components=lock.getJSONArray("components");
            for(int i=0;i<components.length();i++){JSONObject c=components.getJSONObject(i);if(c.optString("name").equals("ubuntu-base-arm64"))expected=c.getString("sha256");}
            if(expected.isEmpty())throw new IOException("缺少运行环境校验信息");
            File archive=new File(offline,"base/base-arm64.tar.gz");
            try(InputStream input=new FileInputStream(archive)){if(!digest(input).equals(expected))throw new IOException("内置运行环境校验失败；请安装完整且未损坏的安装包");}
            File staging=new File(engine,"rootfs-staging");removeEngineTree(staging);staging.mkdirs();
            progress.update("正在解压手机私有运行环境……");
            try(InputStream input=new FileInputStream(archive)){SafeTar.extractGzip(input,staging);}
            write(new File(staging,".base-ready"),expected);
            if(rootfs.exists())throw new IOException("检测到旧运行环境，请保留数据并联系检查，未覆盖");
            Files.move(staging.toPath(),rootfs.toPath(),StandardCopyOption.ATOMIC_MOVE);
        }
        prepareGuest();
        progress.update("正在安装内置组件；仅 QQ 客户端需从腾讯官方下载……");
        Process installer=launch(Arrays.asList("/bin/bash","/opt/mobile/install-runtime.sh"),progress);
        int exit=installer.waitFor();
        synchronized(this){if(process==installer)process=null;}
        if(exit!=0 || !ready())throw new IOException("初始化未完成（退出码 "+exit+"），可查看日志后重试");
        progress.update("运行组件准备完成，请设置新小号和 AI 接口");
    }

    synchronized void start(Progress progress) throws Exception {
        if(running())return;
        if(!ready())throw new IOException("请先完成运行环境初始化");
        copyPayload();prepareOffline(offlineManifest(),progress);prepareGuest();
        launch(Arrays.asList("/home/mobile/tools/bin/python3","/opt/mobile/mobile_control.py"),progress);
    }

    synchronized boolean running(){return process!=null && process.isAlive();}
    synchronized void stop(){
        if(process==null)return;
        process.destroy();
        try{if(!process.waitFor(25,java.util.concurrent.TimeUnit.SECONDS))process.destroyForcibly();}
        catch(InterruptedException e){Thread.currentThread().interrupt();process.destroyForcibly();}
        process=null;
        try{new SecretVault(home,engine).seal();}catch(Exception ignored){/* Keep the only working key; never rotate it on failure. */}
    }

    private synchronized Process launch(List<String> command,Progress progress)throws Exception {
        File nativeDir=new File(context.getApplicationInfo().nativeLibraryDir);
        File executable=new File(nativeDir,"libproot.so"),loader=new File(nativeDir,"libproot-loader.so");
        if(!executable.canExecute()||!loader.isFile())throw new IOException("APK 中的 ARM64 运行组件缺失或不可执行");
        File aliases=new File(engine,"native");aliases.mkdirs();
        link(new File(aliases,"libtalloc.so.2"),new File(nativeDir,"libtalloc.so"));
        link(new File(aliases,"libandroid-shmem.so"),new File(nativeDir,"libandroid-shmem.so"));
        List<String> args=new ArrayList<>();
        args.add(executable.getPath());Collections.addAll(args,"--kill-on-exit","--link2symlink","--sysvipc","-0","-r",rootfs.getPath());
        bind(args,"/dev","/dev");bind(args,"/proc","/proc");bind(args,"/sys","/sys");bind(args,"/system","/system");
        if(new File("/apex").isDirectory())bind(args,"/apex","/apex");
        // PRoot's Android loader keeps its host path while guest paths are translated.
        bind(args,nativeDir.getPath(),nativeDir.getPath());bind(args,aliases.getPath(),aliases.getPath());
        bind(args,new File(scratch,"proot").getPath(),new File(scratch,"proot").getPath());
        bind(args,home.getPath(),"/home/mobile");bind(args,payload.getPath(),"/opt/mobile");bind(args,offline.getPath(),"/opt/offline");
        bind(args,new File(scratch,"tmp").getPath(),"/tmp");bind(args,new File(scratch,"shm").getPath(),"/dev/shm");
        Collections.addAll(args,"-w","/home/mobile","/usr/bin/env","-i","HOME=/home/mobile","USER=root","LOGNAME=root",
            "PATH=/home/mobile/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","LANG=C.UTF-8","TZ=Asia/Shanghai",
            "DAFEIYU_HOME=/home/mobile","DAFEIYU_OFFLINE=/opt/offline","PYTHONIOENCODING=utf-8","TMPDIR=/tmp");
        args.addAll(command);
        ProcessBuilder builder=new ProcessBuilder(args).directory(home).redirectErrorStream(true);
        Map<String,String> env=builder.environment();
        env.put("LD_LIBRARY_PATH",aliases.getPath()+":"+nativeDir.getPath());
        env.put("PROOT_LOADER",loader.getPath());env.put("PROOT_TMP_DIR",new File(scratch,"proot").getPath());
        env.put("PROOT_NO_SECCOMP","1");env.put("TMPDIR",new File(scratch,"tmp").getPath());
        process=builder.start();final Process child=process;
        Thread reader=new Thread(()->{
            try(BufferedReader input=new BufferedReader(new InputStreamReader(child.getInputStream(),StandardCharsets.UTF_8));
                FileOutputStream log=new FileOutputStream(logFile(),true)){
                String line;long written=0;
                while((line=input.readLine())!=null){
                    // Keep credentials/QR URLs out of the native progress view and runtime log.
                    String safe=line.replaceAll("(?i)(token|api_key|authorization|cookie)([\\s:=]+)[^\\s,;]+","$1$2[已隐藏]");
                    byte[] bytes=(safe+"\n").getBytes(StandardCharsets.UTF_8);
                    if(written<4*1024*1024){log.write(bytes);written+=bytes.length;}
                    if(safe.startsWith("正在")||safe.startsWith("运行组件"))progress.update(safe.substring(0,Math.min(160,safe.length())));
                }
            }catch(IOException ignored){}
        },"mobile-runtime-output");reader.setDaemon(true);reader.start();
        return process;
    }

    private static void bind(List<String> args,String from,String to){Collections.addAll(args,"-b",from+":"+to);}
    private static void link(File target,File source)throws IOException{
        Path alias=target.toPath(),actual=source.toPath();
        if(Files.isSymbolicLink(alias)){
            if(Files.readSymbolicLink(alias).equals(actual))return;
            Files.delete(alias); // An app update can move the extracted native libraries.
        }else if(Files.exists(alias,LinkOption.NOFOLLOW_LINKS))throw new IOException("运行组件链接位置已被占用");
        Files.createSymbolicLink(alias,actual);
    }

    void refreshDns() throws Exception {
        List<String> lines=new ArrayList<>();
        ConnectivityManager manager=(ConnectivityManager)context.getSystemService(Context.CONNECTIVITY_SERVICE);
        if(manager!=null){LinkProperties properties=manager.getLinkProperties(manager.getActiveNetwork());
            if(properties!=null)for(InetAddress server:properties.getDnsServers())lines.add("nameserver "+server.getHostAddress());}
        if(lines.isEmpty()){lines.add("nameserver 1.1.1.1");lines.add("nameserver 8.8.8.8");}
        File resolver=new File(rootfs,"etc/resolv.conf");
        if(Files.isSymbolicLink(resolver.toPath()))Files.delete(resolver.toPath());
        write(resolver,String.join("\n",lines)+"\noptions timeout:3 attempts:2\n");
    }

    private void prepareGuest()throws Exception {
        new File(scratch,"tmp").mkdirs();new File(scratch,"shm").mkdirs();new File(scratch,"proot").mkdirs();
        for(String path:Arrays.asList("home/mobile","opt/mobile","opt/offline","dev/shm","tmp","proc","sys","system","apex"))new File(rootfs,path).mkdirs();
        refreshDns();
        new SecretVault(home,engine).open();
        write(new File(rootfs,"etc/apt/apt.conf.d/99mobile"),"APT::Sandbox::User \"root\";\nAcquire::Retries \"2\";\n");
        File credentials=new File(home,"data/app-control.json");
        if(!credentials.isFile()){
            byte[] random=new byte[32];new SecureRandom().nextBytes(random);
            write(credentials,new JSONObject().put("token",Base64.getUrlEncoder().withoutPadding().encodeToString(random)).toString());
        }
    }

    private JSONObject offlineManifest()throws Exception {
        final JSONObject manifest;
        try{manifest=new JSONObject(assetText("runtime/offline/manifest.json"));}
        catch(Exception error){throw new IOException("安装包缺少内置组件清单；请安装完整的内置组件版",error);}
        if(manifest.optInt("schemaVersion")!=1||!manifest.optString("sha256").matches("[a-f0-9]{64}"))
            throw new IOException("内置组件清单格式无效");
        JSONArray files=manifest.getJSONArray("files");
        if(files.length()<7||files.length()>10000)throw new IOException("内置组件清单数量无效");
        Set<String> names=new HashSet<>();boolean deb=false,wheel=false;long total=0;
        for(int i=0;i<files.length();i++){
            JSONObject entry=files.getJSONObject(i);String relative=entry.getString("path");
            SafeTar.member(offline.toPath(),relative);
            if(relative.isEmpty()||relative.indexOf('\n')>=0||relative.indexOf('\r')>=0||relative.matches("(?:.*?/)?\\.\\.?(?:/.*)?")||!names.add(relative))
                throw new IOException("内置组件路径无效或重复");
            long size=entry.getLong("size");
            if(size<=0||size>1024L*1024*1024||!entry.getString("sha256").matches("[a-f0-9]{64}"))throw new IOException("内置组件摘要或大小无效");
            total+=size;if(total>3L*1024*1024*1024)throw new IOException("内置组件总体积超限");
            deb|=relative.startsWith("apt/pool/")&&relative.endsWith(".deb");wheel|=relative.startsWith("wheels/")&&relative.endsWith(".whl");
        }
        if(!names.containsAll(Arrays.asList("base/base-arm64.tar.gz","archives/napcat.zip","models/sensevoice.tar.bz2","apt/sources.list"))||!deb||!wheel)
            throw new IOException("安装包缺少必须的内置运行组件");
        for(String suite:Arrays.asList("noble","noble-updates","noble-security"))if(!names.contains("apt/dists/"+suite+"/InRelease"))throw new IOException("安装包缺少 Ubuntu 官方签名索引");
        if(manifest.has("totalBytes")&&manifest.getLong("totalBytes")!=total)throw new IOException("内置组件总体积记录不符");
        return manifest;
    }

    private static long offlineBytes(JSONObject manifest)throws Exception {
        long total=0;JSONArray files=manifest.getJSONArray("files");for(int i=0;i<files.length();i++)total+=files.getJSONObject(i).getLong("size");return total;
    }

    private void prepareOffline(JSONObject manifest,Progress progress)throws Exception {
        String version=manifest.getString("sha256");File marker=new File(offline,".offline-version");JSONArray files=manifest.getJSONArray("files");
        boolean unchanged=marker.isFile()&&new String(Files.readAllBytes(marker.toPath()),StandardCharsets.UTF_8).equals(version);
        if(unchanged){
            for(int i=0;i<files.length();i++){JSONObject entry=files.getJSONObject(i);File file=new File(offline,entry.getString("path"));if(!file.isFile()||Files.isSymbolicLink(file.toPath())||file.length()!=entry.getLong("size")){unchanged=false;break;}}
            if(unchanged&&new File(offline,".deb-files.list").isFile()&&new File(offline,"manifest.json").isFile())return;
        }
        if(new StatFs(context.getFilesDir().getPath()).getAvailableBytes()<offlineBytes(manifest)+64L*1024*1024)throw new IOException("空间不足，无法展开安装包内置组件");
        File staging=new File(engine,"offline-staging");removeEngineTree(staging);staging.mkdirs();StringBuilder debs=new StringBuilder();
        for(int i=0;i<files.length();i++){
            if(Thread.currentThread().isInterrupted())throw new InterruptedIOException("初始化已取消");
            JSONObject entry=files.getJSONObject(i);String relative=entry.getString("path");File target=SafeTar.member(staging.toPath(),relative).toFile();target.getParentFile().mkdirs();
            progress.update("正在校验内置运行组件 "+(i+1)+" / "+files.length()+"……");
            MessageDigest hash=MessageDigest.getInstance("SHA-256");long total=0,expected=entry.getLong("size");
            try(InputStream input=context.getAssets().open("runtime/offline/"+relative);OutputStream out=new FileOutputStream(target)){
                byte[] b=new byte[65536];int n;while((n=input.read(b))!=-1){if(Thread.currentThread().isInterrupted())throw new InterruptedIOException("初始化已取消");total+=n;if(total>expected)throw new IOException("内置组件大小不符："+relative);out.write(b,0,n);hash.update(b,0,n);}
            }
            if(total!=expected||!hex(hash.digest()).equals(entry.getString("sha256")))throw new IOException("内置组件校验失败："+relative);
            if(relative.startsWith("apt/pool/")&&relative.endsWith(".deb"))debs.append(relative).append('\n');
        }
        write(new File(staging,"manifest.json"),assetText("runtime/offline/manifest.json"));
        write(new File(staging,".deb-files.list"),debs.toString());write(new File(staging,".offline-version"),version);
        removeEngineTree(offline);Files.move(staging.toPath(),offline.toPath(),StandardCopyOption.ATOMIC_MOVE);
    }

    private void copyPayload()throws Exception {
        JSONObject manifest=new JSONObject(assetText("runtime/payload-manifest.json"));
        String version=manifest.getString("sha256");File marker=new File(payload,".payload-version");
        if(marker.isFile()&&new String(Files.readAllBytes(marker.toPath()),StandardCharsets.UTF_8).equals(version))return;
        File staging=new File(engine,"payload-staging");removeEngineTree(staging);staging.mkdirs();
        JSONArray files=manifest.getJSONArray("files");
        for(int i=0;i<files.length();i++){
            JSONObject entry=files.getJSONObject(i);String relative=entry.getString("path");
            File target=SafeTar.member(staging.toPath(),relative).toFile();target.getParentFile().mkdirs();
            try(InputStream input=context.getAssets().open("runtime/payload/"+relative);OutputStream out=new FileOutputStream(target)){
                byte[] bytes=new byte[65536];int n;MessageDigest hash=MessageDigest.getInstance("SHA-256");
                while((n=input.read(bytes))!=-1){out.write(bytes,0,n);hash.update(bytes,0,n);}
                if(!hex(hash.digest()).equals(entry.getString("sha256")))throw new IOException("功能组件校验失败："+relative);
            }
        }
        write(new File(staging,".payload-version"),version);
        removeEngineTree(payload);Files.move(staging.toPath(),payload.toPath(),StandardCopyOption.ATOMIC_MOVE);
    }

    private void removeEngineTree(File file)throws IOException{
        Path base=engine.toPath().toAbsolutePath().normalize(),target=file.toPath().toAbsolutePath().normalize();
        if(!target.startsWith(base)||target.equals(base))throw new IOException("清理范围越界");
        if(!Files.exists(target,LinkOption.NOFOLLOW_LINKS))return;
        Files.walkFileTree(target,new SimpleFileVisitor<Path>(){
            @Override public FileVisitResult visitFile(Path p,BasicFileAttributes a)throws IOException{Files.delete(p);return FileVisitResult.CONTINUE;}
            @Override public FileVisitResult postVisitDirectory(Path p,IOException error)throws IOException{if(error!=null)throw error;Files.delete(p);return FileVisitResult.CONTINUE;}
        });
    }
    private String assetText(String name)throws IOException{try(InputStream input=context.getAssets().open(name)){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[8192];int n;while((n=input.read(b))!=-1)out.write(b,0,n);return out.toString("UTF-8");}}
    private static void write(File file,String value)throws IOException{file.getParentFile().mkdirs();Path tmp=file.toPath().resolveSibling(file.getName()+".tmp");Files.write(tmp,value.getBytes(StandardCharsets.UTF_8));Files.move(tmp,file.toPath(),StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE);file.setReadable(false,false);file.setWritable(false,false);file.setReadable(true,true);file.setWritable(true,true);}
    private static String digest(InputStream input)throws Exception{MessageDigest hash=MessageDigest.getInstance("SHA-256");byte[] b=new byte[65536];int n;while((n=input.read(b))!=-1)hash.update(b,0,n);return hex(hash.digest());}
    private static String hex(byte[] b){StringBuilder out=new StringBuilder();for(byte v:b)out.append(String.format(Locale.ROOT,"%02x",v&255));return out.toString();}
}
