import dev.clipshelf.app.outbox.OutboxPolicy;

/**
 * Host-runnable regression check for outbox/share invariants (no Android, no
 * framework). Run: run.cmd (Windows) or run.sh (POSIX). Exits non-zero on any
 * violated invariant.
 */
public class OutboxPolicyCheck {

    private static int failures = 0;

    public static void main(String[] args) {
        // Share boundary validation mirrors server rules.
        check("null share rejected", "share_empty".equals(OutboxPolicy.validateShare(null)));
        check("blank share rejected", "share_empty".equals(OutboxPolicy.validateShare("   \n ")));
        check("no-url share rejected", "share_no_url".equals(OutboxPolicy.validateShare("just some words")));
        check("ftp-only share rejected", "share_no_url".equals(OutboxPolicy.validateShare("ftp://example.com/x")));
        check("one-url share accepted", OutboxPolicy.validateShare("see https://example.com/a today") == null);
        check("fifty urls accepted",
                OutboxPolicy.validateShare(repeat("https://e.io/", 50)) == null);
        check("fifty-one urls rejected",
                "share_too_many_urls".equals(OutboxPolicy.validateShare(repeat("https://e.io/", 51))));

        // UTF-8 byte bound is measured in bytes, not chars: 'é' is 2 bytes.
        String url = "https://example.com/a "; // 22 bytes
        int budget = 32768 - url.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
        String atLimit = url + "é".repeat(budget / 2); // even count: exactly at limit
        check("text at 32768-byte limit accepted", OutboxPolicy.validateShare(atLimit) == null);
        String overLimit = url + "é".repeat(budget / 2 + 1); // 2 bytes over
        check("text over 32768 bytes rejected",
                "share_too_large".equals(OutboxPolicy.validateShare(overLimit)));

        // Response classification.
        check("200 delivers", OutboxPolicy.DECISION_DELIVER == OutboxPolicy.decide(200));
        check("201 delivers", OutboxPolicy.DECISION_DELIVER == OutboxPolicy.decide(201));
        check("401 pauses auth", OutboxPolicy.DECISION_PAUSE_AUTH == OutboxPolicy.decide(401));
        check("403 pauses auth", OutboxPolicy.DECISION_PAUSE_AUTH == OutboxPolicy.decide(403));
        int[] rejects = {400, 404, 405, 409, 413, 415, 422};
        for (int code : rejects) {
            check(code + " rejects", OutboxPolicy.DECISION_REJECT == OutboxPolicy.decide(code));
        }
        int[] retries = {0, 408, 429, 500, 502, 503};
        for (int code : retries) {
            check(code + " retries", OutboxPolicy.DECISION_RETRY == OutboxPolicy.decide(code));
        }

        // Receipt must belong to this exact row before delivery counts.
        String rowId = "9f0a1b2c-0000-4000-8000-000000000001";
        String rowInstance = "11111111-2222-4333-8444-555555555555";
        String rowUser = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        check("matching receipt delivers",
                OutboxPolicy.receiptMatches(rowId, rowInstance, rowUser, rowId, rowInstance, rowUser));
        check("case-insensitive uuid match",
                OutboxPolicy.receiptMatches(rowId.toUpperCase(), rowInstance.toUpperCase(), rowUser,
                        rowId, rowInstance, rowUser));
        check("mismatched receipt id blocked",
                !OutboxPolicy.receiptMatches("other", rowInstance, rowUser, rowId, rowInstance, rowUser));
        check("mismatched receipt instance blocked",
                !OutboxPolicy.receiptMatches(rowId, "other-instance", rowUser, rowId, rowInstance, rowUser));
        check("mismatched receipt user blocked",
                !OutboxPolicy.receiptMatches(rowId, rowInstance, "other-user", rowId, rowInstance, rowUser));
        check("missing receipt fields blocked",
                !OutboxPolicy.receiptMatches(null, rowInstance, rowUser, rowId, rowInstance, rowUser));
        check("empty receipt fields blocked",
                !OutboxPolicy.receiptMatches("", rowInstance, rowUser, rowId, rowInstance, rowUser));

        // Identity isolation: rows deliver only under their own instance+account.
        check("same identity matches",
                OutboxPolicy.identityMatches(rowInstance, rowUser, rowInstance, rowUser));
        check("other account never matches",
                !OutboxPolicy.identityMatches(rowInstance, rowUser, rowInstance, "ffffffff-1"));
        check("other server never matches",
                !OutboxPolicy.identityMatches(rowInstance, rowUser, "ffffffff-2", rowUser));
        check("null identity never matches",
                !OutboxPolicy.identityMatches(null, rowUser, rowInstance, rowUser));

        if (failures > 0) {
            System.err.println(failures + " check(s) FAILED");
            System.exit(1);
        }
        System.out.println("OutboxPolicyCheck: all invariants hold");
    }

    private static void check(String name, boolean ok) {
        if (!ok) {
            System.err.println("FAIL: " + name);
            failures++;
        }
    }

    /** 50 shares of "https://e.io/1 https://e.io/2 …" style URL runs. */
    private static String repeat(String prefix, int n) {
        StringBuilder sb = new StringBuilder();
        for (int i = 1; i <= n; i++) {
            sb.append(prefix).append(i).append(' ');
        }
        return sb.toString();
    }
}
