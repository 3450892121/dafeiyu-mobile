package app.dafeiyu.mobile;

import java.io.*;
import java.nio.file.*;
import java.util.Arrays;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;

public final class VaultFilesTest {
    private static void require(boolean condition) {
        if (!condition) throw new AssertionError("Vault file preservation failed");
    }

    public static void main(String[] args) throws Exception {
        Path home = Files.createTempDirectory(Paths.get(args[0]), "vault-");
        File raw = home.resolve("instance/data/.secret_key").toFile();
        File wrapped = home.resolve("engine/data-key.wrapped").toFile();
        Files.createDirectories(wrapped.toPath().getParent());
        Files.createDirectories(raw.toPath().getParent());
        VaultFiles.KeySource unused = () -> { throw new AssertionError("Unexpected key generation"); };
        VaultFiles.open(raw, wrapped, unused);
        require(!raw.exists() && !wrapped.exists());

        SecretKey key = KeyGenerator.getInstance("AES").generateKey();
        byte[] original = new byte[32];
        Arrays.fill(original, (byte)19);
        Files.write(raw.toPath(), original);
        VaultFiles.open(raw, wrapped, unused);
        VaultFiles.seal(raw, wrapped, () -> key);
        require(!raw.exists() && wrapped.length() == 60);
        byte[] sealed = Files.readAllBytes(wrapped.toPath());
        // A killed legacy opener could leave a partial raw file beside the only valid wrapped key.
        Files.write(raw.toPath(), new byte[]{1, 2});
        VaultFiles.open(raw, wrapped, () -> key);
        require(Arrays.equals(original, Files.readAllBytes(raw.toPath())));
        require(Arrays.equals(sealed, Files.readAllBytes(wrapped.toPath())));

        byte[] partial = new byte[]{3};
        Files.write(raw.toPath(), partial);
        sealed[sealed.length - 1] ^= 1;
        Files.write(wrapped.toPath(), sealed);
        try {
            VaultFiles.open(raw, wrapped, () -> key);
            throw new AssertionError("Damaged wrapped data accepted");
        } catch (javax.crypto.AEADBadTagException expected) { }
        require(Arrays.equals(partial, Files.readAllBytes(raw.toPath())));
        require(Arrays.equals(sealed, Files.readAllBytes(wrapped.toPath())));
        Files.delete(wrapped.toPath());
        try {
            VaultFiles.open(raw, wrapped, unused);
            throw new AssertionError("Partial key accepted without a backup");
        } catch (IOException expected) { }
        require(Arrays.equals(partial, Files.readAllBytes(raw.toPath())));
        System.out.println("Vault preservation checks passed");
    }
}
