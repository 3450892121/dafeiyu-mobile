package app.dafeiyu.mobile;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Arrays;
import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Keep the previous usable copy until its authenticated replacement is complete. */
final class VaultFiles {
    interface KeySource { SecretKey get() throws Exception; }

    static synchronized void open(File raw, File wrapped, KeySource source) throws Exception {
        if (raw.isFile() && raw.length() >= 32) return; // Legacy readers use the first 32 bytes.
        if (!wrapped.isFile()) {
            if (raw.exists()) throw new IOException("本机解密材料不完整，已保留原文件");
            return;
        }
        byte[] stored = Files.readAllBytes(wrapped.toPath());
        if (stored.length != 12 + 32 + 16) throw new IOException("本机密钥保护文件损坏");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, source.get(), new GCMParameterSpec(128, stored, 0, 12));
        byte[] value = cipher.doFinal(stored, 12, stored.length - 12);
        try {
            Files.createDirectories(raw.toPath().getParent());
            // Both paths are under the app's private files directory. Keep plaintext staging outside the backup tree.
            replace(wrapped.toPath().resolveSibling("data-key.opening"), raw.toPath(), value);
        } finally {
            Arrays.fill(value, (byte)0);
        }
    }

    static synchronized void seal(File raw, File wrapped, KeySource source) throws Exception {
        if (!raw.isFile()) return;
        byte[] value = Files.readAllBytes(raw.toPath());
        try {
            if (value.length != 32) throw new IOException("本机解密材料格式无效，未覆盖");
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, source.get());
            byte[] encrypted = cipher.doFinal(value), iv = cipher.getIV();
            byte[] stored = Arrays.copyOf(iv, iv.length + encrypted.length);
            System.arraycopy(encrypted, 0, stored, iv.length, encrypted.length);
            replace(wrapped.toPath().resolveSibling("data-key.wrapping"), wrapped.toPath(), stored);
            Files.delete(raw.toPath());
        } finally {
            Arrays.fill(value, (byte)0);
        }
    }

    private static void replace(Path staging, Path target, byte[] bytes) throws IOException {
        try {
            Files.write(staging, bytes);
            File file = staging.toFile();
            file.setReadable(false, false);
            file.setWritable(false, false);
            file.setReadable(true, true);
            file.setWritable(true, true);
            Files.move(staging, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } finally {
            Files.deleteIfExists(staging);
        }
    }
}
