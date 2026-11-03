package app.dafeiyu.mobile;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.io.*;
import java.nio.file.*;
import java.security.KeyStore;
import javax.crypto.*;
import javax.crypto.spec.GCMParameterSpec;
import java.util.Arrays;

/** Wrap the guest's data-encryption key with this installation's Android Keystore key. */
final class SecretVault {
    private static final String ALIAS="dafeiyu.mobile.data.v1";
    private final File raw,wrapped;
    SecretVault(File home,File engine){raw=new File(home,"data/.secret_key");wrapped=new File(engine,"data-key.wrapped");}
    private SecretKey key()throws Exception{
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        if(store.containsAlias(ALIAS))return (SecretKey)store.getKey(ALIAS,null);
        if(wrapped.exists())throw new IOException("系统密钥不可用，请使用加密备份恢复资料");
        KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setKeySize(256).build());
        return generator.generateKey();
    }
    void open()throws Exception{
        if(raw.isFile()||!wrapped.isFile())return;
        byte[] stored=Files.readAllBytes(wrapped.toPath());
        if(stored.length<12+16)throw new IOException("本机密钥保护文件损坏");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Arrays.copyOfRange(stored,0,12)));
        byte[] value=cipher.doFinal(Arrays.copyOfRange(stored,12,stored.length));
        if(value.length!=32)throw new IOException("本机解密材料格式无效");
        try{raw.getParentFile().mkdirs();Files.write(raw.toPath(),value);raw.setReadable(false,false);raw.setWritable(false,false);raw.setReadable(true,true);raw.setWritable(true,true);}finally{Arrays.fill(value,(byte)0);}
    }
    void seal()throws Exception{
        if(!raw.isFile())return;
        byte[] value=Files.readAllBytes(raw.toPath());
        if(value.length!=32)throw new IOException("本机解密材料格式无效，未覆盖");
        try{
            Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
            byte[] encrypted=cipher.doFinal(value),iv=cipher.getIV(),output=new byte[iv.length+encrypted.length];
            System.arraycopy(iv,0,output,0,iv.length);System.arraycopy(encrypted,0,output,iv.length,encrypted.length);
            File temp=new File(wrapped.getPath()+".tmp");Files.write(temp.toPath(),output);Files.move(temp.toPath(),wrapped.toPath(),StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE);
            Files.delete(raw.toPath());
        }finally{Arrays.fill(value,(byte)0);}
    }
}
