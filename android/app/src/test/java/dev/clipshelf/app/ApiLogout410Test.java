package dev.clipshelf.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.fail;

import android.content.Context;

import dev.clipshelf.app.net.Api;

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

/**
 * django-allauth answers 410 on DELETE /_allauth/app/v1/auth/session when the
 * token's session no longer exists (password change, session_version bump).
 * That is "already signed out": Api.logout must return, so signOut reports
 * success instead of surfacing the raw 410 JSON body. 401 stays signed-out;
 * other 4xx stay surfaced.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
@ConscryptMode(ConscryptMode.Mode.OFF)
public class ApiLogout410Test {

    private static final String EMAIL = "a@b.c";
    private static final String TOKEN = "tok";

    private Origin origin;
    private String endpoint;
    private Context ctx;

    @Before
    public void setUp() throws Exception {
        origin = new Origin();
        endpoint = "http://127.0.0.1:" + origin.port();
        ctx = RuntimeEnvironment.getApplication();
    }

    @After
    public void tearDown() throws Exception {
        origin.close();
    }

    @Test
    public void logout410CountsAsAlreadySignedOut() throws Exception {
        origin.status = 410;
        Api.logout(endpoint, TOKEN); // must not throw
        assertNull(PendingLogout.signOut(ctx, endpoint, EMAIL, TOKEN));
    }

    @Test
    public void logout401StillCountsAsAlreadySignedOut() throws Exception {
        origin.status = 401;
        Api.logout(endpoint, TOKEN);
        assertNull(PendingLogout.signOut(ctx, endpoint, EMAIL, TOKEN));
    }

    @Test
    public void logout400StillSurfaces() throws Exception {
        origin.status = 400;
        try {
            Api.logout(endpoint, TOKEN);
            fail("400 is a real failure and must surface");
        } catch (Api.ApiException e) {
            assertEquals(400, e.code);
        }
        assertNotNull(PendingLogout.signOut(ctx, endpoint, EMAIL, TOKEN));
    }

    /** Session DELETE origin answering one configurable status and an allauth-shaped body. */
    private static final class Origin {
        volatile int status = 200;
        private final ServerSocket server =
                new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));

        Origin() throws Exception {
            Thread t = new Thread(this::serve, "logout-410-origin");
            t.setDaemon(true);
            t.start();
        }

        int port() {
            return server.getLocalPort();
        }

        void close() throws Exception {
            server.close();
        }

        private void serve() {
            while (!server.isClosed()) {
                try (Socket s = server.accept()) {
                    s.setSoTimeout(10_000);
                    readHead(s.getInputStream());
                    byte[] body = ("{\"status\": " + status + ", \"data\": {\"flows\": "
                            + "[{\"id\": \"login\"}]}}").getBytes(StandardCharsets.UTF_8);
                    OutputStream out = s.getOutputStream();
                    out.write(("HTTP/1.1 " + status + " S\r\nContent-Type: application/json\r\n"
                            + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n")
                            .getBytes(StandardCharsets.US_ASCII));
                    out.write(body);
                    out.flush();
                } catch (Exception e) {
                    // closed by close(), or a broken connection the client reports itself
                }
            }
        }

        private static void readHead(InputStream in) throws Exception {
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int b;
            while ((b = in.read()) != -1) {
                head.write(b);
                if (head.toString("US-ASCII").endsWith("\r\n\r\n")) {
                    return;
                }
            }
        }
    }
}
