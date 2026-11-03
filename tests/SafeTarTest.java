package app.dafeiyu.mobile;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.zip.GZIPOutputStream;

public final class SafeTarTest {
    private static int tests;
    static byte[] archive(String name,byte[] value)throws Exception{
        return archiveEntries(new String[][]{{name,"0",""}},new byte[][]{value});
    }
    static byte[] archiveEntries(String[][] entries,byte[][] contents)throws Exception{
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();
        try(GZIPOutputStream gzip=new GZIPOutputStream(bytes)){
            for(int i=0;i<entries.length;i++){
                byte[] value=contents[i],header=new byte[512];put(header,0,entries[i][0]);put(header,100,"0000700");put(header,124,String.format(Locale.ROOT,"%011o",value.length));header[156]=(byte)entries[i][1].charAt(0);put(header,157,entries[i][2]);
        Arrays.fill(header,148,156,(byte)' ');int sum=0;for(byte b:header)sum+=b&255;put(header,148,String.format(Locale.ROOT,"%06o",sum));header[154]=0;header[155]=' ';
                gzip.write(header);gzip.write(value);gzip.write(new byte[(512-value.length%512)%512]);
            }
            gzip.write(new byte[1024]);
        }
        return bytes.toByteArray();
    }
    static void put(byte[] b,int offset,String text){byte[] v=text.getBytes(StandardCharsets.UTF_8);System.arraycopy(v,0,b,offset,v.length);}
    static void require(boolean condition){tests++;if(!condition)throw new AssertionError("archive contract "+tests);}
    public static void main(String[] args)throws Exception{
        if(args.length!=1)throw new IllegalArgumentException("Pass this project's private test output directory");
        Path root=Files.createTempDirectory(Paths.get(args[0]),"tar-test-").toAbsolutePath();
        byte[] value="新的本机资料".getBytes(StandardCharsets.UTF_8);
        SafeTar.extractGzip(new ByteArrayInputStream(archive("./usr/share/资料.txt",value)),root.toFile());
        require(Arrays.equals(value,Files.readAllBytes(root.resolve("usr/share/资料.txt"))));
        for(String name:new String[]{"../outside","/absolute","foo\\bar","../../reference/project"}){
            boolean rejected=false;try{SafeTar.extractGzip(new ByteArrayInputStream(archive(name,value)),root.toFile());}catch(IOException expected){rejected=true;}require(rejected);
        }
        boolean bad=false;try{SafeTar.extractGzip(new ByteArrayInputStream(new byte[]{1,2,3}),root.toFile());}catch(IOException expected){bad=true;}require(bad);
        require(SafeTar.member(root,"./etc/../usr").equals(root.resolve("usr")));
        byte[] empty=new byte[0];
        Path links=Files.createDirectory(root.resolve("links"));
        SafeTar.extractGzip(new ByteArrayInputStream(archiveEntries(new String[][]{{"usr/bin/perl","0",""},{"usr/bin/perl5","1","usr/bin/perl"}},new byte[][]{value,empty})),links.toFile());
        require(Arrays.equals(value,Files.readAllBytes(links.resolve("usr/bin/perl5"))));
        require(Files.isExecutable(links.resolve("usr/bin/perl5")));
        boolean escape=false;try{SafeTar.extractGzip(new ByteArrayInputStream(archiveEntries(new String[][]{{"copy","1","../outside"}},new byte[][]{empty})),links.toFile());}catch(IOException expected){escape=true;}require(escape);
        boolean overwrite=false;try{SafeTar.extractGzip(new ByteArrayInputStream(archiveEntries(new String[][]{{"usr/bin/perl5","1","usr/bin/perl"}},new byte[][]{empty})),links.toFile());}catch(IOException expected){overwrite=true;}require(overwrite);
        System.out.println("SafeTar checks passed: "+tests);
    }
}
