import java.io.*;
import java.util.*;
import java.util.zip.*;

/** AAPT2 on Windows can use backslashes for nested asset entry names. */
public final class NormalizeZip {
    public static void main(String[] args)throws Exception {
        if(args.length!=2)throw new IllegalArgumentException("input output");
        Set<String> names=new HashSet<>();long total=0;
        try(ZipFile source=new ZipFile(args[0]);ZipOutputStream output=new ZipOutputStream(new FileOutputStream(args[1]))) {
            Enumeration<? extends ZipEntry> entries=source.entries();
            while(entries.hasMoreElements()){
                ZipEntry input=entries.nextElement();String name=input.getName().replace('\\','/');
                if(name.startsWith("/")||Arrays.asList(name.split("/")).contains("..")||!names.add(name))throw new IOException("Invalid/duplicate archive path");
                ByteArrayOutputStream bytes=new ByteArrayOutputStream();
                try(InputStream in=source.getInputStream(input)){byte[] buffer=new byte[65536];int n;while((n=in.read(buffer))!=-1){bytes.write(buffer,0,n);total+=n;if(bytes.size()>64*1024*1024||total>256L*1024*1024)throw new IOException("Resource archive size limit");}}
                byte[] data=bytes.toByteArray();ZipEntry target=new ZipEntry(name);target.setTime(input.getTime());
                if(input.getMethod()==ZipEntry.STORED){CRC32 crc=new CRC32();crc.update(data);target.setMethod(ZipEntry.STORED);target.setSize(data.length);target.setCrc(crc.getValue());}
                output.putNextEntry(target);output.write(data);output.closeEntry();
            }
        }
        System.out.println("Resource ZIP paths normalized: "+names.size());
    }
}
