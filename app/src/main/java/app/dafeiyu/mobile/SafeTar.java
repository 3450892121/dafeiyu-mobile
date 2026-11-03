package app.dafeiyu.mobile;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermission;
import java.util.*;
import java.util.zip.GZIPInputStream;

/** Bounded extraction of a trusted, hash-verified Linux rootfs, retaining guest links. */
public final class SafeTar {
    private static final long MAX_TOTAL=1400L*1024*1024;
    private static final int MAX_ENTRIES=180000;
    private SafeTar(){}

    public static void extractGzip(InputStream compressed,File destination) throws IOException {
        Path root=destination.toPath().toAbsolutePath().normalize();
        Files.createDirectories(root);
        long total=0;int count=0;String longName=null,longLink=null;
        Map<String,String> pax=new HashMap<>();List<String[]> hardLinks=new ArrayList<>();
        try(InputStream input=new BufferedInputStream(new GZIPInputStream(compressed))){
            byte[] header=new byte[512];
            while(readHeader(input,header)){
                if(Thread.currentThread().isInterrupted())throw new InterruptedIOException("初始化已取消");
                if(++count>MAX_ENTRIES)throw new IOException("运行归档文件数量超限");
                long size=number(header,124,12),mode=number(header,100,8);
                int type=header[156];
                String name=string(header,0,100),prefix=string(header,345,155);
                if(!prefix.isEmpty())name=prefix+"/"+name;
                String link=string(header,157,100);
                if(type=='L'||type=='K'||type=='x'||type=='g'){
                    if(size>1024*1024)throw new IOException("归档扩展头超限");
                    byte[] data=readBytes(input,(int)size);skip(input,padding(size));
                    String value=new String(data,StandardCharsets.UTF_8).replace("\0","");
                    if(type=='L')longName=value.trim();else if(type=='K')longLink=value.trim();
                    else if(type=='x')pax=parsePax(data);
                    continue;
                }
                if(longName!=null){name=longName;longName=null;}if(longLink!=null){link=longLink;longLink=null;}
                name=pax.getOrDefault("path",name);link=pax.getOrDefault("linkpath",link);pax.clear();
                Path target=member(root,name);total+=size;
                if(size<0||size>MAX_TOTAL||total>MAX_TOTAL)throw new IOException("运行归档解压超限");
                if(type=='5'){
                    ensureParents(root,target);Files.createDirectories(target);permissions(target,mode);
                    skip(input,size);
                }else if(type=='2'){
                    ensureParents(root,target.getParent());validateGuestLink(root,target,link);
                    if(!Files.exists(target,LinkOption.NOFOLLOW_LINKS))Files.createSymbolicLink(target,Paths.get(link));
                    skip(input,size);
                }else if(type=='1'){
                    hardLinks.add(new String[]{name,link});skip(input,size);
                }else if(type==0||type=='0'||type=='7'){
                    ensureParents(root,target.getParent());
                    if(Files.isSymbolicLink(target))throw new IOException("归档文件覆盖链接");
                    try(OutputStream output=Files.newOutputStream(target)){copy(input,output,size);}
                    permissions(target,mode);
                }else{skip(input,size);} // Device nodes/FIFOs are replaced by explicit private runtime mounts.
                skip(input,padding(size));
            }
        }
        for(String[] pair:hardLinks){
            Path to=member(root,pair[0]),from=member(root,pair[1].replaceFirst("^/+",""));
            ensureParents(root,to.getParent());
            if(!from.toRealPath().startsWith(root.toRealPath()))throw new IOException("归档硬链接越界");
            if(!Files.isRegularFile(from,LinkOption.NOFOLLOW_LINKS))throw new IOException("归档硬链接目标不是普通文件");
            long copied=Files.size(from);
            if(copied>MAX_TOTAL-total)throw new IOException("运行归档解压超限");
            total+=copied;
            // Android app sandboxes may forbid link(2); the base archive's hard links
            // are ordinary files, so materialize them without requiring that syscall.
            Files.copy(from,to,StandardCopyOption.COPY_ATTRIBUTES);
        }
    }

    static Path member(Path root,String name)throws IOException{
        if(name.indexOf('\0')>=0||name.startsWith("/")||name.contains("\\"))throw new IOException("归档路径无效");
        Path relative=Paths.get(name).normalize(),target=root.resolve(relative).normalize();
        if(relative.isAbsolute()||!target.startsWith(root))throw new IOException("归档路径越界");
        return target;
    }
    private static void validateGuestLink(Path root,Path target,String link)throws IOException{
        if(link.isEmpty()||link.indexOf('\0')>=0||link.contains("\\"))throw new IOException("归档链接无效");
        Path logical=link.startsWith("/")?root.resolve(link.substring(1)):target.getParent().resolve(link);
        if(!logical.normalize().startsWith(root))throw new IOException("归档链接越界");
    }
    private static void ensureParents(Path root,Path target)throws IOException{
        if(target==null||!target.normalize().startsWith(root))throw new IOException("归档目录越界");
        Path current=root;
        for(Path part:root.relativize(target)){
            current=current.resolve(part);
            if(Files.exists(current,LinkOption.NOFOLLOW_LINKS)&&Files.isSymbolicLink(current)){
                if(!current.toRealPath().startsWith(root.toRealPath()))throw new IOException("归档目录经链接越界");
            }else Files.createDirectories(current);
        }
    }
    private static boolean readHeader(InputStream input,byte[] header)throws IOException{
        int first=input.read();if(first<0)return false;header[0]=(byte)first;
        readFully(input,header,1,511);boolean empty=true;for(byte b:header)if(b!=0){empty=false;break;}
        if(empty)return false;
        long expected=number(header,148,8),sum=0;
        for(int i=0;i<512;i++)sum+=(i>=148&&i<156)?32:header[i]&255;
        if(sum!=expected)throw new IOException("归档校验头无效");
        return true;
    }
    private static long number(byte[] b,int start,int length)throws IOException{
        if((b[start]&128)!=0)throw new IOException("不接受超大归档数字");
        String s=string(b,start,length).trim();try{return s.isEmpty()?0:Long.parseLong(s,8);}catch(NumberFormatException e){throw new IOException("归档数字无效",e);}
    }
    private static String string(byte[] b,int start,int length){int end=start;while(end<start+length&&b[end]!=0)end++;return new String(b,start,end-start,StandardCharsets.UTF_8);}
    private static long padding(long size){return (512-size%512)%512;}
    private static byte[] readBytes(InputStream in,int size)throws IOException{byte[] b=new byte[size];readFully(in,b,0,size);return b;}
    private static void readFully(InputStream in,byte[] b,int offset,int count)throws IOException{while(count>0){int n=in.read(b,offset,count);if(n<0)throw new EOFException("归档被截断");offset+=n;count-=n;}}
    private static void copy(InputStream in,OutputStream out,long count)throws IOException{byte[] b=new byte[64*1024];while(count>0){int n=in.read(b,0,(int)Math.min(b.length,count));if(n<0)throw new EOFException("归档被截断");out.write(b,0,n);count-=n;}}
    private static void skip(InputStream in,long count)throws IOException{copy(in,new OutputStream(){public void write(int b){}public void write(byte[]b,int o,int n){}},count);}
    private static Map<String,String> parsePax(byte[] data)throws IOException{
        Map<String,String> result=new HashMap<>();int offset=0;
        while(offset<data.length){int space=offset;while(space<data.length&&data[space]!=' ')space++;
            int size;try{size=Integer.parseInt(new String(data,offset,space-offset,StandardCharsets.US_ASCII));}catch(Exception e){throw new IOException("PAX 头无效",e);}
            if(size<=space-offset+1||offset+size>data.length)throw new IOException("PAX 长度无效");
            String record=new String(data,space+1,offset+size-space-2,StandardCharsets.UTF_8);int equal=record.indexOf('=');
            if(equal>0)result.put(record.substring(0,equal),record.substring(equal+1));offset+=size;
        }return result;
    }
    private static void permissions(Path path,long mode)throws IOException{
        try{
            Set<PosixFilePermission> flags=EnumSet.noneOf(PosixFilePermission.class);
            PosixFilePermission[] all={PosixFilePermission.OTHERS_EXECUTE,PosixFilePermission.OTHERS_WRITE,PosixFilePermission.OTHERS_READ,PosixFilePermission.GROUP_EXECUTE,PosixFilePermission.GROUP_WRITE,PosixFilePermission.GROUP_READ,PosixFilePermission.OWNER_EXECUTE,PosixFilePermission.OWNER_WRITE,PosixFilePermission.OWNER_READ};
            for(int i=0;i<all.length;i++)if((mode&(1L<<i))!=0)flags.add(all[i]);Files.setPosixFilePermissions(path,flags);
        }catch(UnsupportedOperationException ignored){path.toFile().setExecutable((mode&0100)!=0,true);}
    }
}
