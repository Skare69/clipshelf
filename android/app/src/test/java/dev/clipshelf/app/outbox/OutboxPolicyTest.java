package dev.clipshelf.app.outbox;

import org.junit.Test;

import java.nio.charset.StandardCharsets;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

/**
 * Outbox/share invariants promised by the README, ported from the former
 * android/hostcheck main() program so CI runs them: share boundary mirrors
 * server rules, 4xx rejects while 408/429 retry, receipts and identities
 * bind to the capturing row, and the whole-outbox cap binds exactly with no
 * eviction.
 */
public class OutboxPolicyTest {

    // --- Share boundary validation (mirrors server rules) ---

    @Test
    public void shareBoundaryValidation() {
        assertEquals("share_empty", OutboxPolicy.validateShare(null));
        assertEquals("share_empty", OutboxPolicy.validateShare("   \n "));
        assertEquals("share_no_url", OutboxPolicy.validateShare("just some words"));
        assertEquals("share_no_url", OutboxPolicy.validateShare("ftp://example.com/x"));
        assertNull(OutboxPolicy.validateShare("see https://example.com/a today"));
        assertNull(OutboxPolicy.validateShare(repeat("https://e.io/", 50)));
        assertEquals("share_too_many_urls", OutboxPolicy.validateShare(repeat("https://e.io/", 51)));
    }

    @Test
    public void angleBracketLinksCountLikeTheServer() {
        // Mail/Markdown "<url1><url2>": the server rule stops at <>"'`,
        // so a pair is two URLs; \S+ would swallow it into one and the
        // phone would admit a share the server later rejects (HTTP 400).
        assertEquals(2, OutboxPolicy.countUrls("<https://a/1><https://a/2>"));
        String pairs = "<https://a/1><https://a/2>".repeat(26); // server: 52 URLs
        assertEquals("share_too_many_urls", OutboxPolicy.validateShare(pairs));
    }

    @Test
    public void textLimitIsUtf8BytesNotChars() {
        // UTF-8 byte bound is measured in bytes, not chars: 'é' is 2 bytes.
        String url = "https://example.com/a "; // 22 bytes
        int budget = OutboxPolicy.MAX_TEXT_BYTES
                - url.getBytes(StandardCharsets.UTF_8).length;
        String atLimit = url + "é".repeat(budget / 2); // even count: exactly at limit
        assertNull(OutboxPolicy.validateShare(atLimit));
        String overLimit = url + "é".repeat(budget / 2 + 1); // 2 bytes over
        assertEquals("share_too_large", OutboxPolicy.validateShare(overLimit));
    }

    // --- Response classification ---

    @Test
    public void successDelivers() {
        assertEquals(OutboxPolicy.DECISION_DELIVER, OutboxPolicy.decide(200));
        assertEquals(OutboxPolicy.DECISION_DELIVER, OutboxPolicy.decide(201));
    }

    @Test
    public void authFailuresPause() {
        assertEquals(OutboxPolicy.DECISION_PAUSE_AUTH, OutboxPolicy.decide(401));
        assertEquals(OutboxPolicy.DECISION_PAUSE_AUTH, OutboxPolicy.decide(403));
    }

    @Test
    public void clientErrorsReject() {
        int[] rejects = {400, 404, 405, 409, 413, 415, 422};
        for (int code : rejects) {
            assertEquals(OutboxPolicy.DECISION_REJECT, OutboxPolicy.decide(code));
        }
    }

    @Test
    public void transportAndTransientErrorsRetry() {
        int[] retries = {0, 408, 429, 500, 502, 503};
        for (int code : retries) {
            assertEquals(OutboxPolicy.DECISION_RETRY, OutboxPolicy.decide(code));
        }
    }

    // --- Receipts must belong to this exact row before delivery counts ---

    @Test
    public void receiptMustMatchRowExactly() {
        String rowId = "9f0a1b2c-0000-4000-8000-000000000001";
        String rowInstance = "11111111-2222-4333-8444-555555555555";
        String rowUser = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        assertTrue(OutboxPolicy.receiptMatches(rowId, rowInstance, rowUser, rowId, rowInstance, rowUser));
        assertTrue(OutboxPolicy.receiptMatches(rowId.toUpperCase(), rowInstance.toUpperCase(), rowUser,
                rowId, rowInstance, rowUser));
        assertFalse(OutboxPolicy.receiptMatches("other", rowInstance, rowUser, rowId, rowInstance, rowUser));
        assertFalse(OutboxPolicy.receiptMatches(rowId, "other-instance", rowUser, rowId, rowInstance, rowUser));
        assertFalse(OutboxPolicy.receiptMatches(rowId, rowInstance, "other-user", rowId, rowInstance, rowUser));
        assertFalse(OutboxPolicy.receiptMatches(null, rowInstance, rowUser, rowId, rowInstance, rowUser));
        assertFalse(OutboxPolicy.receiptMatches("", rowInstance, rowUser, rowId, rowInstance, rowUser));
    }

    // --- Identity isolation: rows deliver only under their own instance+account ---

    @Test
    public void identityIsolation() {
        String rowInstance = "11111111-2222-4333-8444-555555555555";
        String rowUser = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        assertTrue(OutboxPolicy.identityMatches(rowInstance, rowUser, rowInstance, rowUser));
        assertFalse(OutboxPolicy.identityMatches(rowInstance, rowUser, rowInstance, "ffffffff-1"));
        assertFalse(OutboxPolicy.identityMatches(rowInstance, rowUser, "ffffffff-2", rowUser));
        assertFalse(OutboxPolicy.identityMatches(null, rowUser, rowInstance, rowUser));
    }

    // --- Whole-outbox capacity: the cap binds exactly, admission never evicts ---

    @Test
    public void rowCapBindsExactly() {
        assertFalse(OutboxPolicy.outboxFull(0, 0L, OutboxPolicy.MAX_TEXT_BYTES));
        assertFalse(OutboxPolicy.outboxFull(OutboxPolicy.MAX_OUTBOX_ROWS - 1, 0L, 1));
        assertTrue(OutboxPolicy.outboxFull(OutboxPolicy.MAX_OUTBOX_ROWS, 0L, 1));
    }

    @Test
    public void byteCapBindsExactly() {
        long belowBytes = OutboxPolicy.MAX_OUTBOX_TOTAL_TEXT_BYTES - OutboxPolicy.MAX_TEXT_BYTES;
        assertFalse(OutboxPolicy.outboxFull(1, belowBytes, OutboxPolicy.MAX_TEXT_BYTES));
        assertTrue(OutboxPolicy.outboxFull(1, belowBytes + 1, OutboxPolicy.MAX_TEXT_BYTES));
    }

    @Test
    public void offlineGrowthStopsExactlyAtRowCap() {
        // Admit small shares until the outbox is full; rows only ever grow
        // and stop exactly at the cap — no eviction path may silently drop
        // stored shares.
        String grow = "https://e.io/offline-growth";
        int growBytes = grow.getBytes(StandardCharsets.UTF_8).length;
        int grownRows = 0;
        long grownBytes = 0;
        while (!OutboxPolicy.outboxFull(grownRows, grownBytes, growBytes)) {
            grownRows++;
            grownBytes += growBytes;
        }
        assertEquals(OutboxPolicy.MAX_OUTBOX_ROWS, grownRows);
        assertTrue(OutboxPolicy.outboxFull(grownRows, grownBytes, growBytes));
    }

    /** "prefix1 prefix2 … prefixN" — N distinct URLs separated by spaces. */
    private static String repeat(String prefix, int n) {
        StringBuilder sb = new StringBuilder();
        for (int i = 1; i <= n; i++) {
            sb.append(prefix).append(i).append(' ');
        }
        return sb.toString();
    }
}
