package dev.clipshelf.app.work;

import android.content.Context;

import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;

import java.util.concurrent.TimeUnit;

/**
 * Durable scheduling. The periodic reconciliation is registered during account
 * setup — BEFORE the app accepts any share — so a process kill between the
 * local outbox commit and the prompt one-time enqueue cannot strand a saved
 * share until the next app launch. WorkManager persists across process death
 * and reboot.
 */
public final class WorkScheduler {

    public static final String PERIODIC = "clipshelf-reconcile";
    public static final String ONESHOT = "clipshelf-drain";

    private WorkScheduler() {
    }

    private static Constraints network() {
        return new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
    }

    /** Idempotent; safe to call from setup and from every app open (KEEP). */
    public static void ensurePeriodic(Context context) {
        PeriodicWorkRequest req = new PeriodicWorkRequest.Builder(DeliverWorker.class, 3, TimeUnit.HOURS)
                .setConstraints(network())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build();
        WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req);
    }

    /** Prompt delivery right after a share is committed. APPEND_OR_REPLACE:
     * chains behind in-flight drains, replaces failed ones. */
    public static void drainNow(Context context) {
        OneTimeWorkRequest req = new OneTimeWorkRequest.Builder(DeliverWorker.class)
                .setConstraints(network())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build();
        WorkManager.getInstance(context)
                .enqueueUniqueWork(ONESHOT, ExistingWorkPolicy.APPEND_OR_REPLACE, req);
    }
}
