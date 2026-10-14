package dev.clipshelf.app.ui;

import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;
import static org.robolectric.Shadows.shadowOf;

import android.content.Context;
import android.os.Looper;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import androidx.appcompat.widget.Toolbar;

import org.junit.After;
import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.InvalidAlgorithmParameterException;
import java.security.Key;
import java.security.Provider;
import java.security.SecureRandom;
import java.security.Security;
import java.security.cert.Certificate;
import java.security.spec.AlgorithmParameterSpec;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import javax.crypto.KeyGeneratorSpi;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;

/**
 * Settings adopts a new endpoint+token and revokes the old session, and
 * bottom nav returns to the existing LibraryActivity instance without
 * onCreate. Library therefore must re-read the session on every reload:
 * after an endpoint change, refresh must send the new token to the new
 * origin, never the revoked token to the old origin.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class LibrarySessionReloadTest {

    private static final String INSTANCE_ID = "11111111-2222-3333-4444-555555555555";
    private static final String TOKEN_A = "token-a";
    private static final String TOKEN_B = "token-b";

    private static final String ME_JSON = "{\"instance_id\": \"" + INSTANCE_ID + "\","
            + " \"user\": {\"id\": \"42\", \"email\": \"user@example.com\"},"
            + " \"default_collection_id\": \"c1\","
            + " \"collections\": [{\"id\": \"c1\", \"name\": \"Personal\","
            + " \"kind\": \"personal\", \"owner_id\": \"42\", \"can_manage\": true}]}";
    private static final String ENTRIES_JSON = "{\"entries\": [], \"count\": 0}";

    private Context ctx;
    private Origin originA;
    private Origin originB;

    @Before
    public void setUp() throws IOException {
        ctx = RuntimeEnvironment.getApplication();
        originA = new Origin();
        originB = new Origin();
    }

    @After
    public void tearDown() {
        originA.close();
        originB.close();
    }

    /** Saves profile A, loads Library, switches to profile B (new origin+token
     *  as Settings' adoption does), taps refresh: origin B must receive token B. */
    @Test
    public void refreshAfterEndpointChangeSendsTheNewSession() throws Exception {
        Creds.save(ctx, profile(originA.url), TOKEN_A);
        LibraryActivity library = Robolectric.setupActivity(LibraryActivity.class);
        await(originA, "/api/me", TOKEN_A);
        await(originA, "/api/entries", TOKEN_A);
        originA.hits.clear();

        Creds.save(ctx, profile(originB.url), TOKEN_B);
        Toolbar toolbar = library.findViewById(R.id.toolbar);
        toolbar.getMenu().performIdentifierAction(R.id.menu_refresh, 0);

        await(originB, "/api/me", TOKEN_B);
        await(originB, "/api/entries", TOKEN_B);
        assertTrue("the old origin must not be contacted after the change",
                originA.hits.isEmpty());
    }

    private static Creds.Profile profile(String endpoint) {
        return new Creds.Profile(endpoint, INSTANCE_ID, "42", "user@example.com",
                "c1", "Personal");
    }

    @BeforeClass
    public static void installKeystoreStub() {
        // Creds keeps the session token in an AndroidKeyStore AES key; the JVM
        // has no such provider. A minimal in-memory stand-in keeps save/session
        // round-trips honest (generate under the alias, get it back later).
        if (Security.getProvider("AndroidKeyStore") == null) {
            Security.insertProviderAt(new StubKeyStoreProvider(), 1);
        }
    }

    /** In-memory "AndroidKeyStore": KeyStore + AES KeyGenerator sharing one alias→key map. */
    private static final class StubKeyStoreProvider extends Provider {
        static final Map<String, SecretKey> KEYS = new ConcurrentHashMap<>();

        StubKeyStoreProvider() {
            super("AndroidKeyStore", 1.0, "in-memory keystore stub for Robolectric");
        }

        @Override
        public synchronized Service getService(String type, String name) {
            if ("KeyStore".equals(type) && "AndroidKeyStore".equals(name)) {
                return new Service(this, type, name, StubKeyStore.class.getName(), null, null) {
                    @Override
                    public Object newInstance(Object constructorParameter) {
                        return new StubKeyStore();
                    }
                };
            }
            if ("KeyGenerator".equals(type) && KeyProperties.KEY_ALGORITHM_AES.equals(name)) {
                return new Service(this, type, name, StubKeyGenerator.class.getName(), null, null) {
                    @Override
                    public Object newInstance(Object constructorParameter) {
                        return new StubKeyGenerator();
                    }
                };
            }
            return null;
        }
    }

    private static final class StubKeyStore extends java.security.KeyStoreSpi {
        @Override
        public Key engineGetKey(String alias, char[] password) {
            return StubKeyStoreProvider.KEYS.get(alias);
        }

        @Override
        public void engineSetKeyEntry(String alias, Key key, char[] password, Certificate[] chain) {
            StubKeyStoreProvider.KEYS.put(alias, (SecretKey) key);
        }

        @Override
        public boolean engineContainsAlias(String alias) {
            return StubKeyStoreProvider.KEYS.containsKey(alias);
        }

        @Override
        public void engineLoad(InputStream stream, char[] password) {
            // nothing to load: keys live in KEYS for the whole test JVM
        }

        @Override
        public void engineDeleteEntry(String alias) {
            StubKeyStoreProvider.KEYS.remove(alias);
        }

        // Unneeded entries of the KeyStoreSpi surface: this store serves only
        // Creds' get/generate/load flow.
        @Override
        public java.util.Enumeration<String> engineAliases() {
            return Collections.enumeration(StubKeyStoreProvider.KEYS.keySet());
        }

        @Override
        public int engineSize() {
            return StubKeyStoreProvider.KEYS.size();
        }

        @Override
        public Certificate engineGetCertificate(String alias) {
            return null;
        }

        @Override
        public Certificate[] engineGetCertificateChain(String alias) {
            return null;
        }

        @Override
        public java.util.Date engineGetCreationDate(String alias) {
            return null;
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
        public String engineGetCertificateAlias(Certificate cert) {
            return null;
        }

        @Override
        public boolean engineIsKeyEntry(String alias) {
            return StubKeyStoreProvider.KEYS.containsKey(alias);
        }

        @Override
        public boolean engineIsCertificateEntry(String alias) {
            return false;
        }

        @Override
        public boolean engineEntryInstanceOf(String alias, Class<? extends java.security.KeyStore.Entry> type) {
            return false;
        }

        @Override
        public void engineStore(OutputStream stream, char[] password) {
            throw new UnsupportedOperationException();
        }
    }

    private static final class StubKeyGenerator extends KeyGeneratorSpi {
        private String alias;

        @Override
        protected void engineInit(AlgorithmParameterSpec params, SecureRandom random)
                throws InvalidAlgorithmParameterException {
            if (!(params instanceof KeyGenParameterSpec)) {
                throw new InvalidAlgorithmParameterException("stub supports KeyGenParameterSpec only");
            }
            alias = ((KeyGenParameterSpec) params).getKeystoreAlias();
        }

        @Override
        protected SecretKey engineGenerateKey() {
            byte[] raw = new byte[32];
            new SecureRandom().nextBytes(raw);
            SecretKeySpec key = new SecretKeySpec(raw, KeyProperties.KEY_ALGORITHM_AES);
            StubKeyStoreProvider.KEYS.put(alias, key);
            return key;
        }

        @Override
        protected void engineInit(SecureRandom random) {
            throw new UnsupportedOperationException();
        }

        @Override
        protected void engineInit(int keysize, SecureRandom random) {
            throw new UnsupportedOperationException();
        }
    }

    /** {path, X-Session-Token} per request the origin received. */
    private static final class Origin {
        private final HttpServer server;
        final String url;
        final List<String[]> hits = Collections.synchronizedList(new ArrayList<>());

        Origin() throws IOException {
            server = HttpServer.create(
                    new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
            // Debug builds allow plain http to loopback (Api.checkScheme); the
            // unit-test JVM makes the calls with real sockets either way.
            url = "http://127.0.0.1:" + server.getAddress().getPort();
            server.createContext("/", ex -> {
                hits.add(new String[]{ex.getRequestURI().getPath(),
                        ex.getRequestHeaders().getFirst("X-Session-Token")});
                String body = "/api/me".equals(ex.getRequestURI().getPath()) ? ME_JSON : ENTRIES_JSON;
                byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                ex.getResponseHeaders().set("Content-Type", "application/json");
                ex.sendResponseHeaders(200, bytes.length);
                try (OutputStream out = ex.getResponseBody()) {
                    out.write(bytes);
                }
            });
            server.start();
        }

        void close() {
            server.stop(0);
        }
    }

    /** Runs the main looper until the origin has seen path with the token (5s bound). */
    private static void await(Origin origin, String path, String token) throws Exception {
        long deadline = System.currentTimeMillis() + 5_000;
        while (!seen(origin, path, token)) {
            if (System.currentTimeMillis() > deadline) {
                List<String> pretty = new ArrayList<>();
                synchronized (origin.hits) {
                    for (String[] hit : origin.hits) {
                        pretty.add(hit[0] + " token=" + hit[1]);
                    }
                }
                fail(origin.url + path + " never received the session token; saw " + pretty);
            }
            shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(20);
        }
        shadowOf(Looper.getMainLooper()).idle();
    }

    private static boolean seen(Origin origin, String path, String token) {
        synchronized (origin.hits) {
            for (String[] hit : origin.hits) {
                if (path.equals(hit[0]) && token.equals(hit[1])) {
                    return true;
                }
            }
        }
        return false;
    }
}
