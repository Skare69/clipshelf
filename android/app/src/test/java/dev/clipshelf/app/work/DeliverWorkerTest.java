package dev.clipshelf.app.work;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.Context;

import org.junit.AfterClass;
import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.outbox.OutboxStore;

/**
 * Drain invariants against a canned loopback origin (debug loopback http is
 * allowed by Api.checkScheme). Exercises the real POST /api/captures path and
 * the real OutboxStore transitions; the /api/me gate stays above drain().
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class DeliverWorkerTest {

    private static final String I = "inst-1";
    private static final String U = "user-1";

    private static FakeOrigin origin;
    private static String endpoint;

    private OutboxStore db;
    private Creds.Session session;
    private Api.Me me;

    @BeforeClass
    public static void startOrigin() throws Exception {
        origin = new FakeOrigin();
        endpoint = "http://127.0.0.1:" + origin.port();
    }

    @AfterClass
    public static void stopOrigin() throws Exception {
        origin.close();
    }

    @Before
    public void setUp() throws Exception {
        Context ctx = RuntimeEnvironment.getApplication();
        db = new OutboxStore(ctx);
        origin.meIdentity(I, U);
        origin.captureStatus(200);
        origin.captureReceipt("pending", I, U);
        session = new Creds.Session(
                new Creds.Profile(endpoint, I, U, "a@b.c", null, null), "tok");
        me = Api.me(endpoint, session.token);
    }

    private static Context app() {
        return RuntimeEnvironment.getApplication();
    }

    /** Main's generic drain, wired to the real store and deliverOne seam. */
    private int drain() {
        return DeliverWorker.drain(db::listQueuedFor, I, U,
                row -> DeliverWorker.deliverOne(app(), db, session, row));
    }

    private String insertRow() {
        String id = db.insert(I, U, "a@b.c", endpoint, "https://e.io/1", null);
        assertNotNull(id);
        return id;
    }

    /**
     * Defect 2: a receipt mismatch pauses the row, and a later healthy session
     * of the same identity must never silently resurrect and deliver it.
     */
    @Test
    public void mismatchPausedRowStaysPausedOnNextHealthySession() throws Exception {
        String id = insertRow();

        // Run 1: the server answers with a receipt belonging to a different share.
        origin.captureReceipt("some-other-share", I, U);
        drain();
        assertTrue("row must be paused after a mismatch", db.listQueuedFor(I, U, 10).isEmpty());

        // Run 2: healthy session again; the server would even accept the share now.
        db.resetAuthPaused(I, U);
        origin.captureReceipt(id, I, U);
        drain();
        OutboxStore.Row row = db.listNewest(1).get(0);
        assertNotEquals(OutboxStore.STATE_QUEUED, row.state);
        assertNotEquals(OutboxStore.STATE_DELIVERED, row.state);
    }

    /** Auth-paused rows still resume and deliver once the session is healthy. */
    @Test
    public void authPausedRowsResumeAndDeliverOnHealthySession() throws Exception {
        String id = insertRow();

        origin.captureStatus(401);
        drain();
        assertTrue("auth failure must pause the queued row", db.listQueuedFor(I, U, 10).isEmpty());

        db.resetAuthPaused(I, U);
        origin.captureStatus(200);
        origin.captureReceipt(id, I, U);
        drain();
        assertEquals(OutboxStore.STATE_DELIVERED, db.listNewest(1).get(0).state);
    }

    /** Transport failures increment attempts and leave the row queued for retry. */
    @Test
    public void transportFailureIncrementsAttemptsAndKeepsRowQueued() throws Exception {
        insertRow();

        origin.captureStatus(500);
        drain();
        OutboxStore.Row row = db.listNewest(1).get(0);
        assertEquals(OutboxStore.STATE_QUEUED, row.state);
        assertEquals(1, row.attempts);
        assertNotNull(row.lastError);
    }

    /** A matching receipt delivers the row and stores the server receipt. */
    @Test
    public void matchingReceiptDeliversAndStoresReceipt() throws Exception {
        String id = insertRow();
        origin.captureReceipt(id, I, U);

        drain();
        OutboxStore.Row row = db.listNewest(1).get(0);
        assertEquals(OutboxStore.STATE_DELIVERED, row.state);
        assertNotNull(row.receiptJson);
    }

    /** Rows of another identity are never touched by this profile's drain. */
    @Test
    public void drainSkipsOtherIdentities() throws Exception {
        db.insert("other-inst", "other-user", "x@y.z", endpoint, "https://e.io/2", null);
        String mine = insertRow();
        origin.captureReceipt(mine, I, U);

        drain();
        assertEquals(OutboxStore.STATE_DELIVERED, db.listNewest(2).get(0).state);
        assertEquals(OutboxStore.STATE_QUEUED, db.listNewest(2).get(1).state);
    }

    /**
     * Minimal canned HTTP origin for /api/me and /api/captures; unknown paths
     * get 404. Responses carry the receipt/status the test last configured.
     */
    private static final class FakeOrigin {
        private final HttpServer server;
        private volatile String meJson = "{}";
        private volatile int captureStatus = 200;
        private volatile String receiptJson = "{}";

        FakeOrigin() throws Exception {
            server = HttpServer.create(
                    new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0);
            server.createContext("/", this::handle);
            server.start();
        }

        int port() {
            return server.getAddress().getPort();
        }

        void meIdentity(String instanceId, String userId) {
            meJson = "{\"instance_id\":\"" + instanceId + "\",\"user\":{\"id\":\"" + userId
                    + "\",\"email\":\"a@b.c\"},\"collections\":[]}";
        }

        void captureStatus(int status) {
            captureStatus = status;
        }

        void captureReceipt(String clientRequestId, String instanceId, String userId) {
            receiptJson = "{\"receipt\":{\"id\":\"srv-1\",\"client_request_id\":\"" + clientRequestId
                    + "\",\"instance_id\":\"" + instanceId + "\",\"user_id\":\"" + userId + "\"}}";
        }

        void close() {
            server.stop(0);
        }

        private void handle(HttpExchange ex) throws IOException {
            ex.getRequestBody().readAllBytes(); // drain the POST body before answering
            String json;
            int status;
            if ("GET".equals(ex.getRequestMethod()) && "/api/me".equals(ex.getRequestURI().getPath())) {
                status = 200;
                json = meJson;
            } else if ("POST".equals(ex.getRequestMethod())
                    && "/api/captures".equals(ex.getRequestURI().getPath())) {
                status = captureStatus;
                json = receiptJson;
            } else {
                status = 404;
                json = "{}";
            }
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().set("Content-Type", "application/json");
            ex.sendResponseHeaders(status, body.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(body);
            }
        }
    }
}
