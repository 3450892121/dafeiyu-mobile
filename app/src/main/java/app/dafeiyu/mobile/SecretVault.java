package app.dafeiyu.mobile;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.io.*;
import java.security.KeyStore;
import javax.crypto.*;

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
        VaultFiles.open(raw,wrapped,this::key);
    }
    void seal()throws Exception{
        VaultFiles.seal(raw,wrapped,this::key);
    }
}
