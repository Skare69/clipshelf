package dev.clipshelf.app.net;

import org.junit.AfterClass;
import org.junit.BeforeClass;
import org.junit.Test;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.KeyStore;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManagerFactory;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

/**
 * Finding A2 containment, proven against two local HTTPS origins with a
 * sentinel session token: an authenticated cross-origin redirect is rejected
 * and the foreign origin is never contacted, while an external asset URL is
 * fetched without the token. Same-origin redirect chains keep working and
 * keep the token. The keystore is a throwaway self-signed test certificate
 * (SAN 127.0.0.1/localhost) protecting nothing. The origins are tiny
 * SSLServerSocket loops so the test compiles against android.jar and runs on
 * the JVM's real TLS stack.
 */
public class ApiOriginTest {

    private static final String SENTINEL = "sentinel-token";

    private static TlsOrigin originA;
    private static TlsOrigin originB;

    @BeforeClass
    public static void startOrigins() throws Exception {
        SSLContext ssl = sslContext();
        originA = new TlsOrigin(ssl);
        originB = new TlsOrigin(ssl);
        String urlA = originA.url;
        String urlB = originB.url;

        // Origin A: the enrolled origin. /api/me redirects cross-origin.
        originA.redirect("/api/me", urlB + "/steal");
        // Origin A: /asset chains to a same-origin target.
        originA.redirect("/asset", urlA + "/asset2");
        originA.body("/asset2", "chain-ok");

        // Origin B: a foreign origin that must never see the token.
        originB.body("/steal", "stolen");
        originB.body("/asset", "external-asset");
    }

    @AfterClass
    public static void stopOrigins() {
        originA.close();
        originB.close();
    }

    @org.junit.Before
    public void resetHits() {
        originA.reset();
        originB.reset();
    }

    @Test
    public void authenticatedRedirectToForeignOriginIsRejected() throws Exception {
        try {
            Api.me(originA.url, SENTINEL);
            fail("cross-origin redirect on an authenticated request must be rejected");
        } catch (IOException e) {
            assertTrue(e.getMessage(), e.getMessage().contains("cross-origin"));
        }
        // The receiving origin gets no token because it is never contacted.
        assertEquals("[]", originB.hits().toString());
        assertEquals(1, originA.hits().size());
        assertEquals(SENTINEL, originA.hits().get(0)[1]);
    }

    @Test
    public void externalAssetUrlIsFetchedWithoutToken() throws Exception {
        File dest = File.createTempFile("clipshelf-external-asset", ".bin");
        Api.downloadAsset(originA.url, SENTINEL, originB.url + "/asset", dest, 1024);
        assertEquals("external-asset",
                new String(Files.readAllBytes(dest.toPath()), StandardCharsets.UTF_8));
        // The receiving origin saw the request but no token.
        assertEquals(1, originB.hits().size());
        assertNull(originB.hits().get(0)[1]);
        assertTrue(originA.hits().isEmpty());
    }

    @Test
    public void sameOriginRedirectChainKeepsToken() throws Exception {
        File dest = File.createTempFile("clipshelf-chain-asset", ".bin");
        Api.downloadAsset(originA.url, SENTINEL, originA.url + "/asset", dest, 1024);
        assertEquals("chain-ok",
                new String(Files.readAllBytes(dest.toPath()), StandardCharsets.UTF_8));
        assertEquals(2, originA.hits().size());
        assertEquals(SENTINEL, originA.hits().get(0)[1]);
        assertEquals(SENTINEL, originA.hits().get(1)[1]);
        assertTrue(originB.hits().isEmpty());
    }

    // ---- harness ----

    /** Minimal HTTPS origin: records every request's path + session token. */
    private static final class TlsOrigin {
        private final SSLServerSocket listener;
        private final List<String[]> hits =
                Collections.synchronizedList(new java.util.ArrayList<>());
        private final Map<String, String> redirects = new HashMap<>();
        private final Map<String, String> bodies = new HashMap<>();
        final String url;

        TlsOrigin(SSLContext ssl) throws IOException {
            listener = (SSLServerSocket) ssl.getServerSocketFactory()
                    .createServerSocket(0, 16, InetAddress.getByName("127.0.0.1"));
            url = "https://127.0.0.1:" + listener.getLocalPort();
            Thread loop = new Thread(this::serve, "tls-origin-" + listener.getLocalPort());
            loop.setDaemon(true);
            loop.start();
        }

        void redirect(String path, String location) {
            redirects.put(path, location);
        }

        void body(String path, String content) {
            bodies.put(path, content);
        }

        /** {path, X-Session-Token or null} per received request. */
        List<String[]> hits() {
            synchronized (hits) {
                return new java.util.ArrayList<>(hits);
            }
        }

        void reset() {
            hits.clear();
        }

        void close() {
            try {
                listener.close();
            } catch (IOException ignored) {
            }
        }

        private void serve() {
            while (!listener.isClosed()) {
                try (SSLSocket sock = (SSLSocket) listener.accept()) {
                    handle(sock);
                } catch (IOException closed) {
                    return; // listener shut down
                }
            }
        }

        private void handle(SSLSocket sock) throws IOException {
            String head = readHead(sock.getInputStream());
            String path = head.substring(head.indexOf(' ') + 1,
                    head.indexOf(' ', head.indexOf(' ') + 1));
            String token = null;
            for (String line : head.split("\r\n")) {
                int colon = line.indexOf(':');
                if (colon > 0 && "x-session-token"
                        .equals(line.substring(0, colon).trim().toLowerCase(java.util.Locale.ROOT))) {
                    token = line.substring(colon + 1).trim();
                }
            }
            hits.add(new String[]{path, token});
            OutputStream out = sock.getOutputStream();
            String location = redirects.get(path);
            if (location != null) {
                out.write(("HTTP/1.1 302 Found\r\nLocation: " + location
                        + "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                        .getBytes(StandardCharsets.US_ASCII));
            } else {
                byte[] content = bodies.getOrDefault(path, "").getBytes(StandardCharsets.UTF_8);
                out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                        + content.length + "\r\nConnection: close\r\n\r\n")
                        .getBytes(StandardCharsets.US_ASCII));
                out.write(content);
            }
            out.flush();
        }

        private static String readHead(InputStream in) throws IOException {
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int b;
            while ((b = in.read()) != -1) {
                head.write(b);
                byte[] bytes = head.toByteArray();
                int n = bytes.length;
                if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n'
                        && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') {
                    return new String(bytes, StandardCharsets.US_ASCII);
                }
            }
            throw new IOException("Connection closed before request head completed");
        }
    }

    /** One context trusting exactly the throwaway test certificate. */
    private static SSLContext sslContext() throws Exception {
        KeyStore ks = KeyStore.getInstance("PKCS12");
        try (InputStream in = ApiOriginTest.class.getResourceAsStream("/test-keystore.p12")) {
            ks.load(in, "clipshelf-test".toCharArray());
        }
        KeyManagerFactory kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        kmf.init(ks, "clipshelf-test".toCharArray());
        TrustManagerFactory tmf =
                TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(ks);
        SSLContext ssl = SSLContext.getInstance("TLS");
        ssl.init(kmf.getKeyManagers(), tmf.getTrustManagers(), null);
        javax.net.ssl.HttpsURLConnection.setDefaultSSLSocketFactory(ssl.getSocketFactory());
        return ssl;
    }
}
