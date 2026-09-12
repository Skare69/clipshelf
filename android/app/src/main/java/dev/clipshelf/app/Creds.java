package dev.clipshelf.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.security.KeyStore;
import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Profile = endpoint + server instance + account identity (+ default
 * destination). The allauth session token is the only secret kept on the
 * phone; it is AES-GCM encrypted with an Android Keystore key and stored
 * separately from the outbox database. The password is never persisted.
 */
public final class Creds {

    public static final class Profile {
        public final String endpoint, instanceId, userId, email, isAppAdmin;
        public final String defaultCollectionId, defaultCollectionName;

        public Profile(String endpoint, String instanceId, String userId, String email, String isAppAdmin,
                String defaultCollectionId, String defaultCollectionName) {
            this.endpoint = endpoint;
            this.instanceId = instanceId;
            this.userId = userId;
            this.email = email;
            this.isAppAdmin = isAppAdmin;
            this.defaultCollectionId = defaultCollectionId;
            this.defaultCollectionName = defaultCollectionName;
        }
    }

    public static final class Session {
        public final Profile profile;
        public final String token;

        Session(Profile profile, String token) {
            this.profile = profile;
            this.token = token;
        }
    }

    private static final String PREFS = "clipshelf";
    private static final String KEY_ALIAS = "clipshelf.session";
    private static final String K_ENDPOINT = "endpoint";
    private static final String K_INSTANCE = "instance_id";
    private static final String K_USER = "user_id";
    private static final String K_EMAIL = "email";
    private static final String K_ADMIN = "is_app_admin";
    private static final String K_DEFAULT_COL = "default_collection_id";
    private static final String K_DEFAULT_COL_NAME = "default_collection_name";
    private static final String K_TOKEN = "session_token";

    private Creds() {
    }

    public static Profile profile(Context ctx) {
        SharedPreferences p = prefs(ctx);
        String endpoint = p.getString(K_ENDPOINT, null);
        if (endpoint == null) {
            return null;
        }
        return new Profile(endpoint,
                p.getString(K_INSTANCE, ""), p.getString(K_USER, ""),
                p.getString(K_EMAIL, ""), p.getString(K_ADMIN, "false"),
                p.getString(K_DEFAULT_COL, null), p.getString(K_DEFAULT_COL_NAME, ""));
    }

    /** Active session, or null when signed out or the Keystore key was lost/invalidated. */
    public static Session session(Context ctx) {
        Profile profile = profile(ctx);
        if (profile == null) {
            return null;
        }
        String stored = prefs(ctx).getString(K_TOKEN, null);
        if (stored == null) {
            return null;
        }
        try {
            return new Session(profile, decrypt(stored));
        } catch (Exception e) {
            // Key invalidated (e.g. restore on new hardware): drop the unusable
            // token, keep the profile and the outbox untouched; user signs in again.
            prefs(ctx).edit().remove(K_TOKEN).apply();
            return null;
        }
    }

    public static void save(Context ctx, Profile profile, String token) {
        prefs(ctx).edit()
                .putString(K_ENDPOINT, profile.endpoint)
                .putString(K_INSTANCE, profile.instanceId)
                .putString(K_USER, profile.userId)
                .putString(K_EMAIL, profile.email)
                .putString(K_ADMIN, profile.isAppAdmin)
                .putString(K_DEFAULT_COL, profile.defaultCollectionId)
                .putString(K_DEFAULT_COL_NAME, profile.defaultCollectionName)
                .putString(K_TOKEN, encrypt(token))
                .apply();
    }

    /** Hostname/origin change under the same instance: routing only, identity untouched. */
    public static void updateEndpoint(Context ctx, String endpoint) {
        prefs(ctx).edit().putString(K_ENDPOINT, endpoint).apply();
    }

    public static void updateDefaultCollection(Context ctx, String id, String name) {
        prefs(ctx).edit().putString(K_DEFAULT_COL, id).putString(K_DEFAULT_COL_NAME, name).apply();
    }

    /** Sign out: clears profile + token. Outbox rows are deliberately preserved. */
    public static void clear(Context ctx) {
        prefs(ctx).edit().clear().apply();
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ---- Keystore AES-GCM ----

    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        java.security.Key existing = ks.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) {
            return (SecretKey) existing;
        }
        KeyGenerator gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        gen.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return gen.generateKey();
    }

    private static String encrypt(String plain) {
        try {
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.ENCRYPT_MODE, key());
            byte[] iv = c.getIV();
            byte[] ct = c.doFinal(plain.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            byte[] out = new byte[iv.length + ct.length];
            System.arraycopy(iv, 0, out, 0, iv.length);
            System.arraycopy(ct, 0, out, iv.length, ct.length);
            return Base64.encodeToString(out, Base64.NO_WRAP);
        } catch (Exception e) {
            throw new IllegalStateException("Session token encryption failed", e);
        }
    }

    private static String decrypt(String stored) throws Exception {
        byte[] all = Base64.decode(stored, Base64.NO_WRAP);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, all, 0, 12));
        byte[] pt = c.doFinal(all, 12, all.length - 12);
        return new String(pt, java.nio.charset.StandardCharsets.UTF_8);
    }
}
