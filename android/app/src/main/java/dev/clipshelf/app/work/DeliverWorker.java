package dev.clipshelf.app.work;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import java.io.IOException;
import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.PendingLogout;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.outbox.OutboxPolicy;
import dev.clipshelf.app.outbox.OutboxStore;

/**
 * Outbox drain. Safety rules, in order:
 *  1. Only the active profile's identity (instance + user) is delivered; rows
 *     of other accounts/servers are skipped, never rerouted.
 *  2. GET /api/me must confirm the same instance and account before any POST.
 *  3. A receipt counts only when its ids match the row exactly.
 *  4. Auth failures pause the profile's queued rows and keep them; they resume
 *     on successful re-login of the same identity.
 *  5. Transport failures and 5xx retry with WorkManager exponential backoff;
 *     the server dedupes via client_request_id, so a lost response cannot
 *     create a second contribution or reroute an accepted receipt.
 *  6. Endpoint semantics (review A10 decision): identity binds a row, not the
 *     hostname. Delivery posts through the active profile's endpoint, so a
 *     settings-verified host change under the same instance/account moves
 *     queued transport; row.endpoint is capture-time provenance only, and a
 *     different server/account never acquires queued rows.
 */
public class DeliverWorker extends Worker {

    private static final int MAX_ROWS_PER_RUN = 50;
    private static final int BATCH = 10;

    private static final int OUTCOME_CONTINUE = 0;
    private static final int OUTCOME_STOP_TRANSIENT = 1;
    private static final int OUTCOME_STOP_AUTH = 2;
    private static final int OUTCOME_STOP_MISMATCH = 3;

    public DeliverWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        Context ctx = getApplicationContext();
        // Finish any sign-out the network blocked before deciding what to deliver.
        PendingLogout.revokeAll(ctx);
        Creds.Session session = Creds.session(ctx);
        if (session == null) {
            return Result.success(); // signed out (or token invalidated): nothing deliverable
        }
        OutboxStore db = new OutboxStore(ctx);
        Creds.Profile profile = session.profile;

        Api.Me me;
        try {
            me = Api.me(profile.endpoint, session.token);
        } catch (IOException e) {
            if (isAuthFailure(e)) {
                db.pauseAuthFor(profile.instanceId, profile.userId,
                        ctx.getString(R.string.auth_paused_hint));
                return Result.success(); // paused; do not burn retries on auth
            }
            return Result.retry();
        }
        if (!OutboxPolicy.identityMatches(me.instanceId, me.userId, profile.instanceId, profile.userId)) {
            db.pauseAuthFor(profile.instanceId, profile.userId,
                    ctx.getString(R.string.endpoint_mismatch_rejected));
            return Result.success();
        }
        db.resetAuthPaused(me.instanceId, me.userId); // session healthy: resume paused rows

        int drained = drain(db::listQueuedFor, me.instanceId, me.userId,
                row -> deliverOne(ctx, db, session, row));
        if (drained == DRAIN_RETRY) {
            return Result.retry();
        }
        if (drained == DRAIN_BACKLOG) {
            // Clean run hit the per-run cap with rows left: chain the next batch
            // immediately instead of burning exponential backoff. Rows keep their
            // idempotent client_request_id (row.id) across continuations.
            WorkScheduler.drainNow(ctx);
        }
        return Result.success();
    }

    private int deliverOne(Context ctx, OutboxStore db, Creds.Session session, OutboxStore.Row row) {
        try {
            Api.Receipt receipt = Api.capture(session.profile.endpoint, session.token,
                    row.id, row.text, row.requestedCollectionId, row.instanceId, row.userId);
            if (!OutboxPolicy.receiptMatches(receipt.clientRequestId, receipt.instanceId, receipt.userId,
                    row.id, row.instanceId, row.userId)) {
                db.markPaused(row.id, "Server receipt does not match this share; delivery blocked");
                return OUTCOME_STOP_MISMATCH;
            }
            db.markDelivered(row.id, receipt.json);
            return OUTCOME_CONTINUE;
        } catch (IOException e) {
            int decision = OutboxPolicy.decide(e instanceof Api.ApiException ? ((Api.ApiException) e).code : 0);
            switch (decision) {
                case OutboxPolicy.DECISION_PAUSE_AUTH:
                    db.pauseAuthFor(row.instanceId, row.userId,
                            ctx.getString(R.string.auth_paused_hint));
                    return OUTCOME_STOP_AUTH;
                case OutboxPolicy.DECISION_REJECT:
                    db.markRejected(row.id, e.getMessage());
                    return OUTCOME_CONTINUE;
                default:
                    db.addAttempt(row.id, e.getMessage());
                    return OUTCOME_STOP_TRANSIENT;
            }
        }
    }

    private static boolean isAuthFailure(IOException e) {
        return e instanceof Api.ApiException
                && (((Api.ApiException) e).code == 401 || ((Api.ApiException) e).code == 403);
    }

    /** drain() results. */
    public static final int DRAIN_DONE = 0;    // queue empty after the run
    public static final int DRAIN_RETRY = 1;   // transient or stop with rows left: backoff
    public static final int DRAIN_BACKLOG = 2; // clean stop at the cap with rows left

    /** Row source and deliverer seams; generic so JVM tests need no Android classes. */
    public interface Queue<T> {
        List<T> queued(String instanceId, String userId, int limit);
    }

    public interface Deliver<T> {
        int one(T row);
    }

    /**
     * Drains up to MAX_ROWS_PER_RUN queued rows in queue order. Returns
     * DRAIN_RETRY on a transient failure or a stop with rows remaining (caller
     * backs off), DRAIN_BACKLOG when a clean run stops at the cap with rows
     * remaining (caller schedules an immediate continuation), DRAIN_DONE when
     * the queue is drained.
     */
    public static <T> int drain(Queue<T> q, String instanceId, String userId, Deliver<T> deliver) {
        int processed = 0;
        List<T> batch = q.queued(instanceId, userId, BATCH);
        while (!batch.isEmpty() && processed < MAX_ROWS_PER_RUN) {
            boolean stopped = false;
            for (T row : batch) {
                processed++;
                int outcome = deliver.one(row);
                if (outcome == OUTCOME_STOP_TRANSIENT) {
                    return DRAIN_RETRY; // transport/5xx: back off (server dedupes on replay)
                }
                if (outcome != OUTCOME_CONTINUE) {
                    stopped = true;
                    break;
                }
            }
            if (stopped) {
                // Auth pause empties the identity's queue; a mismatch stop can
                // leave sibling rows — retry them on backoff as before.
                return q.queued(instanceId, userId, 1).isEmpty() ? DRAIN_DONE : DRAIN_RETRY;
            }
            batch = q.queued(instanceId, userId, BATCH);
        }
        return batch.isEmpty() ? DRAIN_DONE : DRAIN_BACKLOG;
    }
}
