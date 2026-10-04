package dev.clipshelf.app.outbox;

import android.content.Context;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

/**
 * Persistence invariants for the durable outbox, exercised against real
 * SQLite through Robolectric (no instrumentation device needed).
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class OutboxStoreTest {

    private static final String I = "inst-1";
    private static final String U = "user-1";

    private OutboxStore db;

    @Before
    public void setUp() {
        Context ctx = RuntimeEnvironment.getApplication();
        db = new OutboxStore(ctx);
    }

    private String insertRow(String text) {
        return db.insert(I, U, "a@b.c", "https://e.example", text, null);
    }

    /** A persisted share yields a usable id and is queryable (defect 1 contract). */
    @Test
    public void insertReturnsIdBackedByARow() {
        String id = insertRow("https://e.io/1");
        assertNotNull(id);
        List<OutboxStore.Row> rows = db.listQueuedFor(I, U, 10);
        assertEquals(1, rows.size());
        assertEquals(id, rows.get(0).id);
        assertEquals(OutboxStore.STATE_QUEUED, rows.get(0).state);
    }

    /**
     * Defect 2: a receipt-mismatch pause must survive a later healthy session
     * of the same identity. Only an explicit user requeue may clear it.
     */
    @Test
    public void mismatchPauseIsNotResumedByResetAuthPaused() {
        insertRow("https://e.io/1");
        String id = db.listNewest(1).get(0).id;
        db.markPaused(id, "Server receipt does not match this share; delivery blocked");

        db.resetAuthPaused(I, U); // next healthy session for this identity

        assertTrue("mismatch-paused row must stay paused", db.listQueuedFor(I, U, 10).isEmpty());
        OutboxStore.Row row = db.listNewest(1).get(0);
        assertNotEquals(OutboxStore.STATE_QUEUED, row.state);
        assertNotEquals(OutboxStore.STATE_DELIVERED, row.state);
    }

    /** Defect 2, other side: an auth pause DOES resume on a healthy session. */
    @Test
    public void authPauseIsResumedByResetAuthPaused() {
        insertRow("https://e.io/1");
        db.pauseAuthFor(I, U, "auth paused");

        db.resetAuthPaused(I, U);

        assertEquals(1, db.listQueuedFor(I, U, 10).size());
    }

    /** Defect 2: the user can still requeue a mismatch-paused row explicitly. */
    @Test
    public void requeueClearsMismatchPause() {
        insertRow("https://e.io/1");
        String id = db.listNewest(1).get(0).id;
        db.markPaused(id, "mismatch");

        db.requeue(id);

        assertEquals(1, db.listQueuedFor(I, U, 10).size());
    }

    /** Defect 3: concurrent drains must not lose attempt increments. */
    @Test
    public void concurrentAddAttemptLosesNoIncrements() throws Exception {
        insertRow("https://e.io/1");
        String id = db.listNewest(1).get(0).id;
        int threads = 4;
        int perThread = 50;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        for (int t = 0; t < threads; t++) {
            pool.execute(() -> {
                try {
                    start.await();
                    for (int i = 0; i < perThread; i++) {
                        db.addAttempt(id, "transport error");
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    done.countDown();
                }
            });
        }
        start.countDown();
        assertTrue(done.await(60, TimeUnit.SECONDS));
        pool.shutdown();

        assertEquals(threads * perThread, db.listNewest(1).get(0).attempts);
    }

    /** The receipt stays the durable record of what the server accepted (defect 4 decision). */
    @Test
    public void markDeliveredStoresReceipt() {
        insertRow("https://e.io/1");
        String id = db.listNewest(1).get(0).id;

        db.markDelivered(id, "{\"receipt\":{\"id\":\"srv-1\"}}");

        assertEquals(OutboxStore.STATE_DELIVERED, db.listNewest(1).get(0).state);
        assertEquals("{\"receipt\":{\"id\":\"srv-1\"}}", db.listNewest(1).get(0).receiptJson);
    }
}
