package dev.clipshelf.app.outbox;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * App-private durable outbox. Rows are keyed by the client request UUID and
 * carry the server instance + account identity they were captured for, so an
 * endpoint or account change can never reroute them. Identity binds a row,
 * not the hostname: delivery routes through the active profile's endpoint
 * once it has verified the same instance + account (see DeliverWorker), so a
 * verified host change moves queued transport. The endpoint column is
 * capture-time provenance for the outbox screen, never a routing input.
 * Schema changes must be non-destructive upgrades (ALTER TABLE), never drops
 * or recreations.
 */
public final class OutboxStore extends SQLiteOpenHelper {

    public static final int STATE_QUEUED = OutboxPolicy.STATE_QUEUED;
    public static final int STATE_DELIVERED = OutboxPolicy.STATE_DELIVERED;
    public static final int STATE_REJECTED = OutboxPolicy.STATE_REJECTED;
    public static final int STATE_PAUSED = OutboxPolicy.STATE_PAUSED;
    public static final int STATE_PAUSED_MISMATCH = OutboxPolicy.STATE_PAUSED_MISMATCH;

    private static final String TABLE = "outbox";
    private static final String DDL = "CREATE TABLE " + TABLE + " ("
            + "id TEXT PRIMARY KEY NOT NULL,"            // client_request_id UUID
            + "instance_id TEXT NOT NULL,"
            + "user_id TEXT NOT NULL,"
            + "account_email TEXT NOT NULL,"
            + "endpoint TEXT NOT NULL,"                  // capture-time provenance (outbox screen); never a routing input
            + "text TEXT NOT NULL,"
            + "requested_collection_id TEXT,"
            + "state INTEGER NOT NULL DEFAULT 0,"
            + "attempts INTEGER NOT NULL DEFAULT 0,"
            + "last_error TEXT,"
            // receipt_json is kept by decision (review 8e473566 follow-up): it is the
            // only durable record of what the server accepted — the reconciliation
            // key after a lost response and a support/debug trail. No runtime
            // consumer today, but removal would require a destructive migration.
            + "receipt_json TEXT,"
            + "created_at INTEGER NOT NULL,"
            + "updated_at INTEGER NOT NULL,"
            + "delivered_at INTEGER)";

    public static final class Row {
        public final String id, instanceId, userId, accountEmail, endpoint, text;
        public final String requestedCollectionId, lastError, receiptJson;
        public final int state, attempts;
        public final long createdAt, updatedAt, deliveredAt;

        Row(Cursor c) {
            id = c.getString(0);
            instanceId = c.getString(1);
            userId = c.getString(2);
            accountEmail = c.getString(3);
            endpoint = c.getString(4);
            text = c.getString(5);
            requestedCollectionId = c.getString(6);
            state = c.getInt(7);
            attempts = c.getInt(8);
            lastError = c.getString(9);
            receiptJson = c.getString(10);
            createdAt = c.getLong(11);
            updatedAt = c.getLong(12);
            deliveredAt = c.isNull(13) ? 0L : c.getLong(13);
        }
    }

    public OutboxStore(Context context) {
        super(context, "outbox.db", null, 1);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL(DDL);
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // ponytail: no v2 yet. When schema evolves, append ordered ALTER-only
        // migration steps here (ADD COLUMN etc.). Never drop or recreate; queued
        // shares must survive app upgrades.
    }

    /**
     * Commit a share durably. Returns the stable client request UUID, or null
     * when the row was NOT persisted — callers must treat null as a failure,
     * never as saved.
     */
    public String insert(String instanceId, String userId, String accountEmail,
                         String endpoint, String text, String requestedCollectionId) {
        String id = UUID.randomUUID().toString();
        long now = System.currentTimeMillis();
        ContentValues cv = new ContentValues();
        cv.put("id", id);
        cv.put("instance_id", instanceId);
        cv.put("user_id", userId);
        cv.put("account_email", accountEmail == null ? "" : accountEmail);
        cv.put("endpoint", endpoint);
        cv.put("text", text);
        cv.put("requested_collection_id", requestedCollectionId);
        cv.put("state", STATE_QUEUED);
        cv.put("attempts", 0);
        cv.put("created_at", now);
        cv.put("updated_at", now);
        long rowId = db().insertWithOnConflict(TABLE, null, cv, SQLiteDatabase.CONFLICT_ABORT);
        return rowId != -1 ? id : null;
    }

    private static final String[] COLUMNS = {
            "id", "instance_id", "user_id", "account_email", "endpoint", "text",
            "requested_collection_id", "state", "attempts", "last_error", "receipt_json",
            "created_at", "updated_at", "delivered_at"};

    /** Newest page for the outbox screen, newest first. */
    public List<Row> listNewest(int limit) {
        return query(null, null, "created_at DESC, id DESC LIMIT " + Math.max(1, limit));
    }

    /** Next older page after the (created_at, id) keyset position; stable across deletes. */
    public List<Row> listOlderThan(long createdAt, String id, int limit) {
        return query("created_at < ? OR (created_at = ? AND id < ?)",
                new String[]{String.valueOf(createdAt), String.valueOf(createdAt), id},
                "created_at DESC, id DESC LIMIT " + Math.max(1, limit));
    }

    public List<Row> listQueuedFor(String instanceId, String userId, int limit) {
        return query("instance_id=? AND user_id=? AND state=" + STATE_QUEUED,
                new String[]{instanceId, userId}, "created_at ASC LIMIT " + Math.max(1, limit));
    }

    private List<Row> query(String where, String[] args, String order) {
        List<Row> out = new ArrayList<>();
        try (Cursor c = db().query(TABLE, COLUMNS, where, args, null, null, order)) {
            while (c.moveToNext()) {
                out.add(new Row(c));
            }
        }
        return out;
    }

    /** Whole-outbox usage across identities: [0] rows, [1] summed UTF-8 text bytes. */
    public long[] usage() {
        try (Cursor c = db().rawQuery(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM " + TABLE, null)) {
            return c.moveToFirst() ? new long[]{c.getLong(0), c.getLong(1)} : new long[]{0L, 0L};
        }
    }

    /** Counts for the active profile: index = state constant. */
    public int[] countsFor(String instanceId, String userId) {
        int[] counts = new int[OutboxStore.STATE_PAUSED_MISMATCH + 1];
        try (Cursor c = db().rawQuery("SELECT state, COUNT(*) FROM " + TABLE
                + " WHERE instance_id=? AND user_id=? GROUP BY state", new String[]{instanceId, userId})) {
            while (c.moveToNext()) {
                int s = c.getInt(0);
                if (s >= 0 && s < counts.length) {
                    counts[s] = c.getInt(1);
                }
            }
        }
        return counts;
    }

    public int countOtherIdentities(String instanceId, String userId) {
        try (Cursor c = db().rawQuery("SELECT COUNT(*) FROM " + TABLE
                        + " WHERE NOT (instance_id=? AND user_id=?)",
                new String[]{instanceId, userId})) {
            return c.moveToFirst() ? c.getInt(0) : 0;
        }
    }

    public void markDelivered(String id, String receiptJson) {
        update(id, STATE_DELIVERED, null, receiptJson, System.currentTimeMillis());
    }

    public void markRejected(String id, String error) {
        update(id, STATE_REJECTED, error, null, 0);
    }

    /**
     * Receipt-mismatch pause: the server answered with a receipt that does not
     * belong to this row. Distinct from an auth pause — a healthy session must
     * never auto-resume it (resetAuthPaused only resumes STATE_PAUSED); only an
     * explicit user requeue clears it.
     */
    public void markPaused(String id, String error) {
        update(id, STATE_PAUSED_MISMATCH, error, null, 0);
    }

    /** Atomic per-row attempt bump; safe under concurrent drains (no read-modify-write). */
    public void addAttempt(String id, String error) {
        db().execSQL("UPDATE " + TABLE + " SET attempts = attempts + 1, last_error = ?,"
                        + " updated_at = ? WHERE id = ?",
                new Object[]{error, System.currentTimeMillis(), id});
    }

    /** User-visible retry for paused/rejected rows. */
    public void requeue(String id) {
        update(id, STATE_QUEUED, null, null, 0);
    }

    /**
     * Session healthy again for this identity: resume auth-paused rows only.
     * STATE_PAUSED_MISMATCH rows (receipt mismatch) stay paused — a healthy
     * session is not evidence the mismatch was resolved.
     */
    public void resetAuthPaused(String instanceId, String userId) {
        ContentValues cv = new ContentValues();
        cv.put("state", STATE_QUEUED);
        cv.putNull("last_error");
        cv.put("updated_at", System.currentTimeMillis());
        db().update(TABLE, cv, "instance_id=? AND user_id=? AND state=" + STATE_PAUSED,
                new String[]{instanceId, userId});
    }

    /**
     * Auth failure pauses this profile's queued rows; rows are preserved, never
     * dropped. These rows resume via resetAuthPaused when the session is healthy.
     */
    public void pauseAuthFor(String instanceId, String userId, String error) {
        ContentValues cv = new ContentValues();
        cv.put("state", STATE_PAUSED);
        cv.put("last_error", error);
        cv.put("updated_at", System.currentTimeMillis());
        db().update(TABLE, cv, "instance_id=? AND user_id=? AND state=" + STATE_QUEUED,
                new String[]{instanceId, userId});
    }

    public int deleteDelivered() {
        return db().delete(TABLE, "state=" + STATE_DELIVERED, null);
    }

    /** Explicit user delete of a single row (the UI offers it for rejected/paused rows). */
    public int delete(String id) {
        return db().delete(TABLE, "id=?", new String[]{id});
    }

    private void update(String id, int state, String error, String receiptJson, long deliveredAt) {
        ContentValues cv = new ContentValues();
        cv.put("state", state);
        cv.put("last_error", error);
        if (receiptJson != null) {
            cv.put("receipt_json", receiptJson);
        }
        if (deliveredAt > 0) {
            cv.put("delivered_at", deliveredAt);
        } else {
            cv.putNull("delivered_at");
        }
        cv.put("updated_at", System.currentTimeMillis());
        db().update(TABLE, cv, "id=?", new String[]{id});
    }

    private SQLiteDatabase db() {
        return getWritableDatabase();
    }
}
