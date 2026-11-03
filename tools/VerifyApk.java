import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.regex.*;
import java.util.zip.*;

/** Inspect the actual signed artifact, not merely the prepackaging asset folder. */
public final class VerifyApk {
    public static void main(String[] args)throws Exception {
        if(args.length!=2)throw new IllegalArgumentException("APK payload-manifest");
        try(ZipFile zip=new ZipFile(args[0])){
            Enumeration<? extends ZipEntry> entries=zip.entries();
            while(entries.hasMoreElements()){
                String name=entries.nextElement().getName();
                if(name.contains("\\")||name.contains("../")||name.matches("(?i).*(?:/|^)(?:bot\\.db|app-control\\.json|\\.secret_key|[^/]*\\.jks|base-arm64\\.tar\\.gz)$"))throw new IOException("Invalid or private APK entry: "+name);
            }
            for(String name:new String[]{"classes.dex","AndroidManifest.xml","resources.arsc","assets/features.json","assets/licenses.txt","assets/runtime/layout.json","assets/runtime/payload-manifest.json","lib/arm64-v8a/libproot.so","lib/arm64-v8a/libproot-loader.so","lib/arm64-v8a/libtalloc.so","lib/arm64-v8a/libandroid-shmem.so"})
                if(zip.getEntry(name)==null)throw new IOException("Missing APK entry: "+name);
            String manifest=new String(Files.readAllBytes(Paths.get(args[1])),StandardCharsets.UTF_8);
            Matcher files=Pattern.compile("\"path\"\\s*:\\s*\"([^\"]+)\"\\s*,\\s*\"sha256\"\\s*:\\s*\"([a-f0-9]{64})\"").matcher(manifest);
            int count=0;
            while(files.find()){
                String name="assets/runtime/payload/"+files.group(1);ZipEntry entry=zip.getEntry(name);
                if(entry==null)throw new IOException("Missing runtime asset: "+name);
                MessageDigest digest=MessageDigest.getInstance("SHA-256");
                try(InputStream in=zip.getInputStream(entry)){byte[] b=new byte[65536];int n;while((n=in.read(b))!=-1)digest.update(b,0,n);}
                StringBuilder actual=new StringBuilder();for(byte b:digest.digest())actual.append(String.format(Locale.ROOT,"%02x",b&255));
                if(!actual.toString().equals(files.group(2)))throw new IOException("Runtime asset hash mismatch: "+name);count++;
            }
            if(count<80)throw new IOException("Incomplete runtime payload manifest");
            System.out.println("Final APK runtime assets verified: "+count+"; all entry paths use forward slashes; no private data bundled");
        }
    }
}
