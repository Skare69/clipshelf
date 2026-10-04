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
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

/**
 * Finding A2 containment, proven against two local HTTPS origins with a
 * sentinel session token: an authenticated cross-origin redirect is rejected
 * and the foreign origin is never contacted, while an external asset URL is
 * fetched without the token. Same-origin redirect chains keep working and
 * keep the token. Endpoint-change adoption is proven the same way: a
 * candidate that copies the public instance id only ever captures
 * credentials it issued itself. The keystore is a throwaway self-signed test
 * certificate
 * (SAN 127.0.0.1/localhost) protecting nothing. The origins are tiny
 * SSLServerSocket loops so the test compiles against android.jar and runs on
 * the JVM's real TLS stack.
 */
public class ApiOriginTest {

    private static final String SENTINEL = "sentinel-token";
    private static final String INSTANCE_ID = "11111111-2222-3333-4444-555555555555";
    private static final String USER_ID = "42";
    private static final String EMAIL = "user@example.com";
    private static final String PASSWORD = "correct horse battery staple";

    private static SSLContext ssl;
    private static TlsOrigin originA;
    private static TlsOrigin originB;

    @BeforeClass
    public static void startOrigins() throws Exception {
        ssl = sslContext();
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

        // Origin B as an endpoint-change candidate: it copies the public
        // instance id of the enrolled instance and answers a login and /api/me
        // plausibly. Endpoint-change tests drive the adoption flow against it.
        originB.body("/api/instance", "{\"instance_id\": \"" + INSTANCE_ID + "\"}");
        originB.body("/_allauth/app/v1/auth/login",
                "{\"meta\": {\"session_token\": \"fresh-token\"}}");
        originB.body("/api/me", meBody());
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
    public void htmlErrorsKeepStatusAndDoNotLeakResponseBodies() throws Exception {
        String bodyMarker = "<html>response-body-marker</html>";
        for (int code : new int[]{401, 404}) {
            originA.response("/api/collections", code + (code == 401 ? " Unauthorized" : " Not Found"),
                    "text/html", bodyMarker, true);
            try {
                Api.collections(originA.url, SENTINEL);
                fail("HTML error responses must preserve their HTTP status");
            } catch (Api.ApiException e) {
                assertEquals(code, e.code);
                assertEquals("Server error " + code, e.getMessage());
                assertFalse(e.getMessage().contains(bodyMarker));
            }
        }
    }

    @Test
    public void streamingAssetAtLimitWritesTheExactBody() throws Exception {
        originA.response("/asset-exact", "200 OK", "application/octet-stream", "four", false);
        File dest = File.createTempFile("clipshelf-bounded-asset", ".bin");
        Api.downloadAsset(originA.url, SENTINEL, "/asset-exact", dest, 4);
        assertEquals(4, Files.size(dest.toPath()));
        assertEquals("four", new String(Files.readAllBytes(dest.toPath()), StandardCharsets.UTF_8));
    }

    @Test
    public void oversizedStreamingAssetDoesNotWriteTheLookaheadByte() throws Exception {
        originA.response("/asset-over", "200 OK", "application/octet-stream", "fives", false);
        File dest = File.createTempFile("clipshelf-oversized-asset", ".bin");
        try {
            Api.downloadAsset(originA.url, SENTINEL, "/asset-over", dest, 4);
            fail("oversized asset must be rejected");
        } catch (IOException e) {
            assertTrue(e.getMessage(), e.getMessage().contains("size limit"));
        }
        assertEquals(4, Files.size(dest.toPath()));
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

    @Test
    public void impostorCopyingInstanceIdNeverReceivesSessionToken() throws Exception {
        // An impostor on a different origin copies the public instance id and
        // answers login and /api/me plausibly. Adoption may only authenticate
        // by what the candidate itself issues: every credential the impostor
        // captures must be the token it minted, never an enrolled one.
        Api.Adopted adopted = Api.adoptCandidate(originB.url, INSTANCE_ID, EMAIL, PASSWORD);
        assertEquals("fresh-token", adopted.token);
        for (String[] hit : originB.hits()) {
            assertTrue("unexpected credential captured via " + hit[0],
                    hit[1] == null || "fresh-token".equals(hit[1]));
        }
        assertTrue(originA.hits().isEmpty());
    }

    @Test
    public void endpointChangeSwapsToFreshCandidateSession() throws Exception {
        // Honest new host for the same instance. The caller persists and
        // reloads the in-memory session from the adopted result, so the
        // adopted pair must be the candidate's address plus the token the
        // candidate issued — never the enrolled session (the old flow kept a
        // stale in-memory endpoint and reused the old token).
        Api.Adopted adopted = Api.adoptCandidate(originB.url, INSTANCE_ID, EMAIL, PASSWORD);
        assertEquals(originB.url, adopted.endpoint);
        assertEquals("fresh-token", adopted.token);
        assertEquals(INSTANCE_ID, adopted.me.instanceId);
        assertEquals(USER_ID, adopted.me.userId);
        assertEquals(EMAIL, adopted.me.email);
        for (String[] hit : originB.hits()) {
            assertTrue(hit[1] == null || "fresh-token".equals(hit[1]));
        }
        assertTrue(originA.hits().isEmpty());
    }

    @Test
    public void candidateForDifferentInstanceIsRefusedBeforeCredentials() throws Exception {
        TlsOrigin impostor = new TlsOrigin(ssl);
        try {
            impostor.body("/api/instance", "{\"instance_id\": \"other-instance\"}");
            Api.adoptCandidate(impostor.url, INSTANCE_ID, EMAIL, PASSWORD);
            fail("a candidate serving a different instance must be refused");
        } catch (IOException e) {
            assertTrue(e.getMessage(), e.getMessage().contains("different instance"));
        } finally {
            impostor.close();
        }
        // Only the unauthenticated probe left the phone: no login attempt, no token.
        assertEquals(1, impostor.hits().size());
        assertNull(impostor.hits().get(0)[1]);
    }

    /** /api/me payload for the enrolled identity, as the server shapes it. */
    private static String meBody() {
        return "{\"instance_id\": \"" + INSTANCE_ID + "\", \"user\": {\"id\": \"" + USER_ID
                + "\", \"email\": \"" + EMAIL + "\"}, \"default_collection_id\": null,"
                + " \"collections\": []}";
    }

    // ---- harness ----

    /** Minimal HTTPS origin: records every request's path + session token. */
    private static final class TlsOrigin {
        private final SSLServerSocket listener;
        private final List<String[]> hits =
                Collections.synchronizedList(new java.util.ArrayList<>());
        private final Map<String, String> redirects = new HashMap<>();
        private final Map<String, String> bodies = new HashMap<>();
        private final Map<String, byte[]> responses =
                Collections.synchronizedMap(new HashMap<>());
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

        void response(String path, String status, String contentType, String content,
                      boolean includeContentLength) {
            byte[] body = content.getBytes(StandardCharsets.UTF_8);
            String head = "HTTP/1.1 " + status + "\r\nContent-Type: " + contentType + "\r\n"
                    + (includeContentLength ? "Content-Length: " + body.length + "\r\n" : "")
                    + "Connection: close\r\n\r\n";
            byte[] header = head.getBytes(StandardCharsets.US_ASCII);
            byte[] wire = new byte[header.length + body.length];
            System.arraycopy(header, 0, wire, 0, header.length);
            System.arraycopy(body, 0, wire, header.length, body.length);
            responses.put(path, wire);
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
            byte[] response = responses.get(path);
            if (response != null) {
                out.write(response);
            } else {
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
            }
            out.flush();
            // Drain whatever the client sent after the head (a POST body).
            // Closing with unread bytes queued would RST the connection and
            // can kill the client's response read.
            try {
                sock.setSoTimeout(250);
                InputStream rest = sock.getInputStream();
                while (rest.read() != -1) {
                    // discard
                }
            } catch (IOException ignored) {
                // client is gone or slow; the response is already out
            }
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
