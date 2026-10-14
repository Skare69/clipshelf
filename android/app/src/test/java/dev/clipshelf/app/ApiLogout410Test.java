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

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
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
        private final HttpServer server;

        Origin() throws Exception {
            server = HttpServer.create(
                    new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
            server.createContext("/", ex -> {
                byte[] body = ("{\"status\": " + status + ", \"data\": {\"flows\": "
                        + "[{\"id\": \"login\"}]}}").getBytes(StandardCharsets.UTF_8);
                ex.getResponseHeaders().set("Content-Type", "application/json");
                ex.sendResponseHeaders(status, body.length);
                try (OutputStream out = ex.getResponseBody()) {
                    out.write(body);
                }
            });
            server.start();
        }

        int port() {
            return server.getAddress().getPort();
        }

        void close() {
            server.stop(0);
        }
    }
}
