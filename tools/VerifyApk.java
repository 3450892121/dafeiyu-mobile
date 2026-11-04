import java.io.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.*;

/** Stream-check the actual signed artifact, including its separately manifested large assets. */
public final class VerifyApk {
    private static final long OFFLINE_LIMIT=1800L*1024*1024;
    private static final int MANIFEST_LIMIT=8*1024*1024;
    private static final String OFFLINE="assets/runtime/offline/", PAYLOAD="assets/runtime/payload/";

    public static void main(String[] args)throws Exception {
        if(args.length!=3)throw new IllegalArgumentException("APK payload-manifest offline-manifest");
        byte[] payloadBytes=smallFile(args[1]),offlineBytes=smallFile(args[2]);
        Map<String,Object> payload=object(new Json(new String(payloadBytes,StandardCharsets.UTF_8)).parse());
        Map<String,Object> offline=object(new Json(new String(offlineBytes,StandardCharsets.UTF_8)).parse());
        List<Object> offlineFiles=array(offline.get("files"));
        if(number(offline.get("schemaVersion"))!=1||offlineFiles.isEmpty()||offlineFiles.size()>10000)
            throw new IOException("Invalid offline manifest schema/files");
        verifyManifestDigest(offline,offlineFiles);
        Set<String> offlineNames=new HashSet<>(),caseNames=new HashSet<>();
        String previous="";long total=0;boolean signedIndex=false,packageIndex=false,debs=false,wheels=false;
        for(Object value:offlineFiles){
            Map<String,Object> file=object(value);String name=string(file.get("path"));long size=number(file.get("size"));
            if(!allowedOffline(name)||name.compareTo(previous)<=0||!caseNames.add(name.toLowerCase(Locale.ROOT))||size<=0||!digest(file.get("sha256")))
                throw new IOException("Invalid, duplicate or unsorted offline path/size/hash: "+name);
            previous=name;offlineNames.add(name);total+=size;
            if(total<0||total>OFFLINE_LIMIT)throw new IOException("Offline assets exceed single-APK size budget");
            URI source=new URI(string(file.get("source")));String host=source.getHost();
            if(!"https".equals(source.getScheme())||host==null||source.getRawUserInfo()!=null||source.getRawQuery()!=null||source.getRawFragment()!=null||
                host.equalsIgnoreCase("qq.com")||host.toLowerCase(Locale.ROOT).endsWith(".qq.com"))
                throw new IOException("Invalid or non-redistributable QQ offline source: "+name);
            signedIndex|=name.matches("apt/dists/.+/InRelease");packageIndex|=name.matches("apt/dists/.+/Packages\\.xz");
            debs|=name.startsWith("apt/pool/")&&name.endsWith(".deb");wheels|=name.startsWith("wheels/")&&name.endsWith(".whl");
        }
        for(String required:new String[]{"base/base-arm64.tar.gz","archives/napcat.zip","models/sensevoice.tar.bz2","apt/sources.list"})
            if(!offlineNames.contains(required))throw new IOException("Missing mandatory offline asset: "+required);
        if(!signedIndex||!packageIndex||!debs||!wheels||number(offline.get("totalBytes"))!=total)
            throw new IOException("Incomplete offline dependency groups or total size mismatch");

        try(ZipFile zip=new ZipFile(args[0])){
            Set<String> archiveNames=new HashSet<>();
            Enumeration<? extends ZipEntry> entries=zip.entries();
            while(entries.hasMoreElements()){
                ZipEntry entry=entries.nextElement();String name=entry.getName(),plain=name.endsWith("/")?name.substring(0,name.length()-1):name;
                if(!safeRelative(plain)||!archiveNames.add(name)||privateEntry(plain)||qqArchive(plain))throw new IOException("Invalid/private/QQ APK entry: "+name);
                if(name.endsWith("base-arm64.tar.gz")&&!name.equals(OFFLINE+"base/base-arm64.tar.gz"))throw new IOException("Unexpected base archive location");
                if(!entry.isDirectory()&&name.startsWith(OFFLINE)&&!name.equals(OFFLINE+"manifest.json")&&!offlineNames.contains(name.substring(OFFLINE.length())))
                    throw new IOException("Unlisted offline APK asset: "+name);
            }
            for(String name:new String[]{"classes.dex","AndroidManifest.xml","resources.arsc","assets/features.json","assets/licenses.txt","assets/runtime/layout.json","assets/runtime/payload-manifest.json",OFFLINE+"manifest.json","lib/arm64-v8a/libproot.so","lib/arm64-v8a/libproot-loader.so","lib/arm64-v8a/libtalloc.so","lib/arm64-v8a/libandroid-shmem.so"})
                if(zip.getEntry(name)==null)throw new IOException("Missing APK entry: "+name);
            if(!Arrays.equals(payloadBytes,smallEntry(zip,"assets/runtime/payload-manifest.json"))||!Arrays.equals(offlineBytes,smallEntry(zip,OFFLINE+"manifest.json")))
                throw new IOException("Final APK manifest differs from the verified build manifest");
            List<Object> payloadFiles=array(payload.get("files"));verifyManifestDigest(payload,payloadFiles);
            Set<String> payloadNames=new HashSet<>();
            for(Object value:payloadFiles){
                Map<String,Object> file=object(value);String name=string(file.get("path"));
                if(!safeRelative(name)||!payloadNames.add(name)||!digest(file.get("sha256")))throw new IOException("Invalid/duplicate runtime payload path/hash");
                verifyEntry(zip,PAYLOAD+name,-1,string(file.get("sha256")),false);
            }
            if(payloadFiles.size()<80)throw new IOException("Incomplete runtime payload manifest");
            for(String name:archiveNames)if(name.startsWith(PAYLOAD)&&!name.endsWith("/")&&!payloadNames.contains(name.substring(PAYLOAD.length())))
                throw new IOException("Unlisted runtime payload: "+name);
            for(Object value:offlineFiles){
                Map<String,Object> file=object(value);
                verifyEntry(zip,OFFLINE+string(file.get("path")),number(file.get("size")),string(file.get("sha256")),true);
            }
            System.out.println("Final APK runtime assets verified: "+payloadFiles.size()+"; all entry paths use forward slashes; no private data bundled");
            System.out.println("Final APK offline assets verified: "+offlineFiles.size()+"; bytes: "+total+"; no QQ archive bundled");
        }
    }

    private static boolean safeRelative(String name){
        if(name.isEmpty()||name.startsWith("/")||name.contains("\\")||name.contains(":"))return false;
        for(String part:name.split("/",-1))if(part.isEmpty()||part.equals(".")||part.equals(".."))return false;
        return true;
    }
    private static boolean allowedOffline(String name){
        return safeRelative(name)&&!qqArchive(name)&&(name.matches("(?:base/base-arm64\\.tar\\.gz|archives/napcat\\.zip|models/sensevoice\\.tar\\.bz2|apt/sources\\.list)")||
            name.matches("apt/dists/[A-Za-z0-9_.+~%/-]+/(?:InRelease|Packages\\.xz)")||name.matches("apt/pool/[A-Za-z0-9_.+~%/-]+\\.deb")||name.matches("wheels/[A-Za-z0-9_.+~%-]+\\.whl"));
    }
    private static boolean qqArchive(String name){return name.matches("(?i).*(?:/|^)(?:linux)?qq[^/]*\\.(?:deb|zip|tar(?:\\.gz|\\.xz|\\.bz2)?)$");}
    private static boolean privateEntry(String name){
        if(name.matches("(?i).*(?:/|^)(?:[^/]*\\.(?:db|sqlite|sqlite3)(?:-wal|-shm)?|app-control\\.json|cookies\\.json|\\.env(?:\\..*)?|\\.secret_key|credentials[^/]*|[^/]*\\.(?:jks|keystore|key|pem|p12|pfx|dafm))$"))return true;
        for(String part:name.split("/"))if(part.equals(".git")||part.equals(".local")||part.equals(".tools")||part.equals("notes")||part.equals("qq-profile")||part.equals("nt_db"))return true;
        return false;
    }
    private static byte[] smallFile(String name)throws IOException{
        Path file=Paths.get(name);if(Files.size(file)>MANIFEST_LIMIT)throw new IOException("Manifest exceeds size limit");return Files.readAllBytes(file);
    }
    private static byte[] smallEntry(ZipFile zip,String name)throws IOException{
        ZipEntry entry=zip.getEntry(name);if(entry==null||entry.getSize()>MANIFEST_LIMIT)throw new IOException("Missing/oversized manifest: "+name);
        ByteArrayOutputStream result=new ByteArrayOutputStream();
        try(InputStream in=zip.getInputStream(entry)){byte[] b=new byte[65536];int n;while((n=in.read(b))!=-1){if(result.size()+n>MANIFEST_LIMIT)throw new IOException("Manifest exceeds size limit");result.write(b,0,n);}}
        return result.toByteArray();
    }
    private static void verifyEntry(ZipFile zip,String name,long expectedSize,String expectedHash,boolean stored)throws Exception{
        ZipEntry entry=zip.getEntry(name);if(entry==null||entry.isDirectory())throw new IOException("Missing APK asset: "+name);
        if(expectedSize>=0&&entry.getSize()!=expectedSize)throw new IOException("APK asset size mismatch: "+name);
        if(stored&&entry.getMethod()!=ZipEntry.STORED)throw new IOException("Large offline asset must be stored without recompression: "+name);
        MessageDigest digest=MessageDigest.getInstance("SHA-256");long size=0;
        try(InputStream in=zip.getInputStream(entry)){byte[] b=new byte[65536];int n;while((n=in.read(b))!=-1){size+=n;if(expectedSize>=0&&size>expectedSize)throw new IOException("APK asset exceeds declared size: "+name);digest.update(b,0,n);}}
        if((expectedSize>=0&&size!=expectedSize)||!hex(digest.digest()).equals(expectedHash))throw new IOException("APK asset size/hash mismatch: "+name);
    }
    private static void verifyManifestDigest(Map<String,Object> manifest,List<Object> files)throws Exception{
        String expected=string(manifest.get("sha256"));if(!digest(expected)||!hex(MessageDigest.getInstance("SHA-256").digest(json(files).getBytes(StandardCharsets.UTF_8))).equals(expected))
            throw new IOException("Manifest files digest mismatch");
    }
    private static String hex(byte[] bytes){StringBuilder out=new StringBuilder();for(byte value:bytes)out.append(String.format(Locale.ROOT,"%02x",value&255));return out.toString();}
    private static boolean digest(Object value){return value instanceof String&&((String)value).matches("[a-f0-9]{64}");}
    @SuppressWarnings("unchecked") private static Map<String,Object> object(Object value)throws IOException{if(!(value instanceof Map))throw new IOException("Expected manifest object");return (Map<String,Object>)value;}
    @SuppressWarnings("unchecked") private static List<Object> array(Object value)throws IOException{if(!(value instanceof List))throw new IOException("Expected manifest array");return (List<Object>)value;}
    private static String string(Object value)throws IOException{if(!(value instanceof String))throw new IOException("Expected manifest string");return (String)value;}
    private static long number(Object value)throws IOException{if(!(value instanceof Long))throw new IOException("Expected integral manifest size/version");return ((Long)value).longValue();}

    // Preserve object insertion order and emit JSON.stringify-compatible strings and integral metadata.
    private static String json(Object value)throws IOException{
        if(value==null)return "null";if(value instanceof Long||value instanceof Boolean)return value.toString();
        if(value instanceof String){String text=(String)value;StringBuilder out=new StringBuilder("\"");for(int i=0;i<text.length();i++){char c=text.charAt(i);switch(c){case '"':out.append("\\\"");break;case '\\':out.append("\\\\");break;case '\b':out.append("\\b");break;case '\f':out.append("\\f");break;case '\n':out.append("\\n");break;case '\r':out.append("\\r");break;case '\t':out.append("\\t");break;default:if(c<32||(Character.isSurrogate(c)&&!(Character.isHighSurrogate(c)&&i+1<text.length()&&Character.isLowSurrogate(text.charAt(i+1)))&&!(Character.isLowSurrogate(c)&&i>0&&Character.isHighSurrogate(text.charAt(i-1)))))out.append(String.format(Locale.ROOT,"\\u%04x",(int)c));else out.append(c);}}return out.append('"').toString();}
        if(value instanceof List){StringBuilder out=new StringBuilder("[");for(Object item:(List<?>)value){if(out.length()>1)out.append(',');out.append(json(item));}return out.append(']').toString();}
        if(value instanceof Map){StringBuilder out=new StringBuilder("{");for(Map.Entry<?,?> item:((Map<?,?>)value).entrySet()){if(out.length()>1)out.append(',');out.append(json(item.getKey())).append(':').append(json(item.getValue()));}return out.append('}').toString();}
        throw new IOException("Unsupported manifest metadata");
    }
    private static final class Json{
        private final String text;private int at=0;
        Json(String text){this.text=text;}
        Object parse()throws IOException{Object result=value(0);space();if(at!=text.length())throw new IOException("Trailing manifest data");return result;}
        private void space(){while(at<text.length()&&" \n\r\t".indexOf(text.charAt(at))>=0)at++;}
        private Object value(int depth)throws IOException{
            if(depth>40)throw new IOException("Manifest nesting too deep");space();if(at>=text.length())throw new IOException("Truncated manifest");char c=text.charAt(at);
            if(c=='"')return quoted();
            if(c=='{'){at++;Map<String,Object> out=new LinkedHashMap<>();space();if(take('}'))return out;do{space();String key=quoted();space();need(':');if(out.containsKey(key))throw new IOException("Duplicate manifest key");out.put(key,value(depth+1));space();if(take('}'))return out;need(',');}while(true);}
            if(c=='['){at++;List<Object> out=new ArrayList<>();space();if(take(']'))return out;do{out.add(value(depth+1));if(out.size()>10000)throw new IOException("Manifest array too long");space();if(take(']'))return out;need(',');}while(true);}
            for(String literal:new String[]{"true","false","null"})if(text.startsWith(literal,at)){at+=literal.length();return literal.equals("null")?null:Boolean.valueOf(literal);}
            int start=at;if(c=='-')at++;if(at>=text.length()||!Character.isDigit(text.charAt(at)))throw new IOException("Invalid manifest value");if(text.charAt(at)=='0')at++;else while(at<text.length()&&Character.isDigit(text.charAt(at)))at++;
            try{return Long.valueOf(text.substring(start,at));}catch(NumberFormatException error){throw new IOException("Invalid manifest integer");}
        }
        private boolean take(char c){if(at<text.length()&&text.charAt(at)==c){at++;return true;}return false;}
        private void need(char c)throws IOException{if(!take(c))throw new IOException("Invalid manifest syntax");}
        private String quoted()throws IOException{
            need('"');StringBuilder out=new StringBuilder();while(at<text.length()){char c=text.charAt(at++);if(c=='"')return out.toString();if(c<32)throw new IOException("Control character in manifest string");if(c!='\\'){out.append(c);continue;}if(at>=text.length())break;c=text.charAt(at++);switch(c){case '"':case '\\':case '/':out.append(c);break;case 'b':out.append('\b');break;case 'f':out.append('\f');break;case 'n':out.append('\n');break;case 'r':out.append('\r');break;case 't':out.append('\t');break;case 'u':if(at+4>text.length())throw new IOException("Truncated Unicode escape");try{out.append((char)Integer.parseInt(text.substring(at,at+4),16));}catch(NumberFormatException error){throw new IOException("Invalid Unicode escape");}at+=4;break;default:throw new IOException("Invalid manifest escape");}}throw new IOException("Unterminated manifest string");
        }
    }
}
