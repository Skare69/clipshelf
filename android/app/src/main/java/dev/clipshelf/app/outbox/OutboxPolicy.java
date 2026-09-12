package dev.clipshelf.app.outbox;

import java.nio.charset.StandardCharsets;
import java.util.regex.Pattern;

/**
 * Pure delivery/share policy with no Android imports, so the invariants are
 * checkable on a host JVM (see android/hostcheck). Mirrors server rules:
 * text <= 32768 UTF-8 bytes, >= 1 http(s) URL, <= 50 URLs.
 */
public final class OutboxPolicy {
    public static final int STATE_QUEUED = 0;
    public static final int STATE_DELIVERED = 1;
    public static final int STATE_REJECTED = 2;
    public static final int STATE_PAUSED = 3;

    public static final int DECISION_DELIVER = 0;
    public static final int DECISION_RETRY = 1;
    public static final int DECISION_REJECT = 2;
    public static final int DECISION_PAUSE_AUTH = 3;

    public static final int MAX_TEXT_BYTES = 32768;
    public static final int MAX_URLS = 50;

    public static final Pattern URL_PATTERN =
            Pattern.compile("https?://\\S+", Pattern.CASE_INSENSITIVE);

    private OutboxPolicy() {
    }

    /**
     * Classify a POST /api/captures outcome. httpStatus 0 means transport-level
     * failure (timeout, connection reset, truncated response) — the request may
     * or may not have reached the server, so the only safe move is to retry the
     * same client_request_id (server dedupes and re-returns the receipt).
     */
    public static int decide(int httpStatus) {
        if (httpStatus == 200 || httpStatus == 201) {
            return DECISION_DELIVER;
        }
        if (httpStatus == 401 || httpStatus == 403) {
            return DECISION_PAUSE_AUTH;
        }
        if (httpStatus == 400 || httpStatus == 404 || httpStatus == 405
                || httpStatus == 409 || httpStatus == 413 || httpStatus == 415 || httpStatus == 422) {
            return DECISION_REJECT;
        }
        return DECISION_RETRY;
    }

    /** A receipt counts as delivery only when it belongs to this exact row. */
    public static boolean receiptMatches(String receiptRequestId, String receiptInstanceId, String receiptUserId,
                                         String rowRequestId, String rowInstanceId, String rowUserId) {
        return sameId(receiptRequestId, rowRequestId)
                && sameId(receiptInstanceId, rowInstanceId)
                && sameId(receiptUserId, rowUserId);
    }

    /** A row may be delivered only under the server instance and account it was captured for. */
    public static boolean identityMatches(String rowInstanceId, String rowUserId,
                                          String profileInstanceId, String profileUserId) {
        return sameId(rowInstanceId, profileInstanceId) && sameId(rowUserId, profileUserId);
    }

    /**
     * External share boundary validation. Returns a strings.xml key
     * (share_empty / share_too_large / share_no_url / share_too_many_urls)
     * or null when the share may be persisted. Server validation stays authoritative.
     */
    public static String validateShare(String text) {
        if (text == null || text.trim().isEmpty()) {
            return "share_empty";
        }
        int bytes = text.getBytes(StandardCharsets.UTF_8).length;
        if (bytes > MAX_TEXT_BYTES) {
            return "share_too_large";
        }
        int urls = countUrls(text);
        if (urls == 0) {
            return "share_no_url";
        }
        if (urls > MAX_URLS) {
            return "share_too_many_urls";
        }
        return null;
    }

    public static int countUrls(String text) {
        java.util.regex.Matcher m = URL_PATTERN.matcher(text);
        int n = 0;
        while (m.find()) {
            n++;
        }
        return n;
    }

    private static boolean sameId(String a, String b) {
        return a != null && !a.isEmpty() && b != null && a.equalsIgnoreCase(b);
    }
}
