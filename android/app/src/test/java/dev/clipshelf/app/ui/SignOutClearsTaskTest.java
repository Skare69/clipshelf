package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.app.Application;
import android.app.Dialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.os.Looper;

import androidx.appcompat.app.AlertDialog;

import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.annotation.Config;
import org.robolectric.shadows.ShadowDialog;
import org.robolectric.shadows.ShadowLooper;

import java.io.InputStream;
import java.io.OutputStream;
import java.security.Key;
import java.security.KeyStoreSpi;
import java.security.NoSuchAlgorithmException;
import java.security.Provider;
import java.security.Security;
import java.security.cert.Certificate;
import java.util.Collections;
import java.util.Date;
import java.util.Enumeration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;

/**
 * Sign-out from Settings starts setup over a task that still holds
 * LibraryActivity: Ui.bottomNav finishes every screen but Library, and
 * Library keeps the session token it read in onCreate. The started intent
 * must therefore carry NEW_TASK|CLEAR_TASK so the whole task — the
 * signed-out library, whose token stays valid while server revocation is
 * pending — is destroyed and Back from setup exits the app. Regression: the
 * intent used to carry no flags, leaving the stale library reachable.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class SignOutClearsTaskTest {

    private Context ctx;

    /**
     * Robolectric implements no AndroidKeyStore, but Creds stores the session
     * token Keystore-encrypted, so without a provider the sign-out screens
     * cannot hold a session at all. This stand-in answers every alias with
     * one in-memory AES key; the cipher work stays real JCE.
     */
    public static final class MemoryKeyStoreProvider extends Provider {
        public MemoryKeyStoreProvider() {
            super("ClipshelfTestKeyStore", 1.0, "in-memory AndroidKeyStore stand-in");
            put("KeyStore.AndroidKeyStore", MemoryKeyStoreSpi.class.getName());
        }
    }

    public static final class MemoryKeyStoreSpi extends KeyStoreSpi {
        private static final Map<String, Key> ENTRIES = new ConcurrentHashMap<>();

        private static SecretKey key() {
            try {
                KeyGenerator g = KeyGenerator.getInstance("AES");
                g.init(256);
                return g.generateKey();
            } catch (NoSuchAlgorithmException e) {
                throw new IllegalStateException(e);
            }
        }

        @Override
        public Key engineGetKey(String alias, char[] password) {
            return ENTRIES.computeIfAbsent(alias, a -> key());
        }

        @Override
        public void engineSetKeyEntry(String alias, Key key, char[] password,
                Certificate[] chain) {
            ENTRIES.put(alias, key);
        }

        @Override
        public void engineLoad(InputStream stream, char[] password) {
        }

        @Override
        public void engineSetKeyEntry(String alias, byte[] key, Certificate[] chain) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void engineSetCertificateEntry(String alias, Certificate cert) {
        }

        @Override
        public Certificate engineGetCertificate(String alias) {
            return null;
        }

        @Override
        public Date engineGetCreationDate(String alias) {
            return null;
        }

        @Override
        public void engineDeleteEntry(String alias) {
            ENTRIES.remove(alias);
        }

        @Override
        public Enumeration<String> engineAliases() {
            return Collections.enumeration(ENTRIES.keySet());
        }

        @Override
        public boolean engineContainsAlias(String alias) {
            return ENTRIES.containsKey(alias);
        }

        @Override
        public int engineSize() {
            return ENTRIES.size();
        }

        @Override
        public boolean engineIsKeyEntry(String alias) {
            return ENTRIES.containsKey(alias);
        }

        @Override
        public boolean engineIsCertificateEntry(String alias) {
            return false;
        }

        @Override
        public String engineGetCertificateAlias(Certificate cert) {
            return null;
        }

        @Override
        public Certificate[] engineGetCertificateChain(String alias) {
            return null;
        }

        @Override
        public void engineStore(OutputStream stream, char[] password) {
        }
    }

    @BeforeClass
    public static void installKeyStore() {
        if (Security.getProvider("ClipshelfTestKeyStore") == null) {
            Security.insertProviderAt(new MemoryKeyStoreProvider(), 1);
        }
    }

    @Before
    public void setUp() {
        ctx = RuntimeEnvironment.getApplication();
        Creds.save(ctx, new Creds.Profile(
                "http://127.0.0.1:1", "i", "u", "a@b.c", null, null), "tok");
        assertNotNull("keystore stand-in must round-trip the token", Creds.session(ctx));
    }

    @Test
    public void signOutStartsSetupWithClearTask() throws Exception {
        // Library is alive in the task (the nav keeps it below Settings) and
        // holds the token it read in onCreate.
        LibraryActivity library = Robolectric.setupActivity(LibraryActivity.class);
        assertFalse("library must stay alive", library.isFinishing());
        SettingsActivity settings = Robolectric.setupActivity(SettingsActivity.class);
        assertFalse("settings must stay alive", settings.isFinishing());
        Shadows.shadowOf((Application) ctx).clearNextStartedActivities();

        settings.findViewById(R.id.settings_logout).performClick();
        List<Dialog> shown = ShadowDialog.getShownDialogs();
        AlertDialog dialog = (AlertDialog) shown.get(shown.size() - 1);
        assertNotNull("logout must ask for confirmation", dialog);
        dialog.getButton(DialogInterface.BUTTON_POSITIVE).performClick();

        // The callback runs on the main looper after the revoke job's real
        // background thread returns (connection refused on port 1 — the
        // callback fires on every outcome).
        Intent started = null;
        for (int i = 0; i < 1000 && started == null; i++) {
            Shadows.shadowOf(Looper.getMainLooper()).idle();
            started = Shadows.shadowOf((Application) ctx).getNextStartedActivity();
            if (started == null) {
                Thread.sleep(5);
            }
        }
        assertNotNull("sign-out must start setup", started);
        assertEquals(SetupActivity.class.getName(), started.getComponent().getClassName());
        assertTrue("setup must clear the task stack",
                (started.getFlags() & Intent.FLAG_ACTIVITY_CLEAR_TASK) != 0);
        assertTrue("CLEAR_TASK requires NEW_TASK",
                (started.getFlags() & Intent.FLAG_ACTIVITY_NEW_TASK) != 0);
        assertNull("sign-out must clear the local identity", Creds.profile(ctx));
    }
}
