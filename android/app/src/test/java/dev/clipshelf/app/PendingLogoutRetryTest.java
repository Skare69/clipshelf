package dev.clipshelf.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.content.Context;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.ConscryptMode;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.Key;
import java.security.KeyStoreSpi;
import java.security.Provider;
import java.security.Security;
import java.security.cert.Certificate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Date;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import javax.crypto.spec.SecretKeySpec;

/**
 * Two pending revocations for the same endpoint and email are separate server
 * sessions: confirming one must keep the other queued until it is confirmed.
 * Robolectric has no AndroidKeyStore, so a test-only provider hands Creds a
 * fixed AES key; the real AES-GCM path still runs. Conscrypt stays off: once
 * installed it is JVM-global and breaks ApiOriginTest's JDK TLS origins.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
@ConscryptMode(ConscryptMode.Mode.OFF)
public class PendingLogoutRetryTest {

    private static final String PROVIDER = "FakeAndroidKeyStore";
    private static final String EMAIL = "a@b.c";

    private LogoutOrigin origin;
    private String endpoint;

    @Before
    public void setUp() throws Exception {
        Provider p = new Provider(PROVIDER, 1.0, "test-only AndroidKeyStore") {
            {
                putService(new Service(this, "KeyStore", "AndroidKeyStore",
                        FixedKeyStore.class.getName(), null, null) {
                    @Override
                    public Object newInstance(Object param) {
                        return new FixedKeyStore();
                    }
                });
            }
        };
        Security.addProvider(p);
        origin = new LogoutOrigin();
        endpoint = "http://127.0.0.1:" + origin.port();
    }

    @After
    public void tearDown() throws Exception {
        origin.close();
        Security.removeProvider(PROVIDER);
    }

    @Test
    public void confirmingOneSessionKeepsTheOtherForRetry() throws Exception {
        Context ctx = RuntimeEnvironment.getApplication();
        origin.accept(); // server down: both sign-outs are kept
        PendingLogout.signOut(ctx, endpoint, EMAIL, "tok-a");
        PendingLogout.signOut(ctx, endpoint, EMAIL, "tok-b");
        assertEquals(Arrays.asList(EMAIL, EMAIL), PendingLogout.pendingLabels(ctx));
        String stored = ctx.getSharedPreferences("clipshelf.revoke", Context.MODE_PRIVATE)
                .getString("pending", "");
        assertFalse("tokens must stay encrypted at rest",
                stored.contains("tok-a") || stored.contains("tok-b"));

        origin.accept("tok-a");
        PendingLogout.revokeAll(ctx);
        assertEquals(Collections.singletonList(EMAIL), PendingLogout.pendingLabels(ctx));

        origin.accept("tok-a", "tok-b");
        origin.hits.clear();
        PendingLogout.revokeAll(ctx);
        assertEquals("the surviving record is retried", Collections.singletonList("tok-b"), origin.hits);
        assertTrue(PendingLogout.pendingLabels(ctx).isEmpty());
    }

    @Test
    public void immediateConfirmationKeepsNothing() throws Exception {
        Context ctx = RuntimeEnvironment.getApplication();
        origin.accept("tok-a");
        assertNull(PendingLogout.signOut(ctx, endpoint, EMAIL, "tok-a"));
        assertTrue(PendingLogout.pendingLabels(ctx).isEmpty());
    }

    /** Session DELETE origin: 200 for accepted tokens, 503 otherwise; records every token. */
    private static final class LogoutOrigin {
        final List<String> hits = Collections.synchronizedList(new ArrayList<>());
        private final ServerSocket server = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
        private volatile Set<String> accepted = new HashSet<>();

        LogoutOrigin() throws Exception {
            Thread t = new Thread(this::serve, "logout-origin");
            t.setDaemon(true);
            t.start();
        }

        int port() {
            return server.getLocalPort();
        }

        void accept(String... tokens) {
            accepted = new HashSet<>(Arrays.asList(tokens));
        }

        void close() throws Exception {
            server.close();
        }

        private void serve() {
            while (!server.isClosed()) {
                try (Socket s = server.accept()) {
                    s.setSoTimeout(10_000);
                    String token = null;
                    for (String line : readHead(s.getInputStream()).split("\r\n")) {
                        if (line.toLowerCase(Locale.ROOT).startsWith("x-session-token:")) {
                            token = line.substring(16).trim();
                        }
                    }
                    hits.add(token);
                    int status = accepted.contains(token) ? 200 : 503;
                    OutputStream out = s.getOutputStream();
                    out.write(("HTTP/1.1 " + status + " T\r\nContent-Type: application/json\r\n"
                            + "Content-Length: 2\r\nConnection: close\r\n\r\n{}")
                            .getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                } catch (Exception e) {
                    // closed by close(), or a broken connection the client reports itself
                }
            }
        }

        private static String readHead(InputStream in) throws Exception {
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int b;
            while ((b = in.read()) != -1) {
                head.write(b);
                String h = head.toString("US-ASCII");
                if (h.endsWith("\r\n\r\n")) {
                    return h;
                }
            }
            return head.toString("US-ASCII");
        }
    }

    /** Minimal AndroidKeyStore stand-in: every alias resolves to one fixed AES key. */
    public static final class FixedKeyStore extends KeyStoreSpi {
        private static final Key KEY = new SecretKeySpec(new byte[32], "AES");

        @Override
        public Key engineGetKey(String alias, char[] password) {
            return KEY;
        }

        @Override
        public Certificate[] engineGetCertificateChain(String alias) {
            return null;
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
        public void engineSetKeyEntry(String alias, Key key, char[] password, Certificate[] chain) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void engineSetKeyEntry(String alias, byte[] key, Certificate[] chain) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void engineSetCertificateEntry(String alias, Certificate cert) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void engineDeleteEntry(String alias) {
            throw new UnsupportedOperationException();
        }

        @Override
        public Enumeration<String> engineAliases() {
            return Collections.emptyEnumeration();
        }

        @Override
        public boolean engineContainsAlias(String alias) {
            return true;
        }

        @Override
        public int engineSize() {
            return 1;
        }

        @Override
        public boolean engineIsKeyEntry(String alias) {
            return true;
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
        public void engineStore(OutputStream stream, char[] password) {
        }

        @Override
        public void engineLoad(InputStream stream, char[] password) {
        }
    }
}
