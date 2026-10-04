package dev.clipshelf.app.work;

/**
 * Error boundaries around the durable share commit and its accelerators.
 * The periodic drain (registered at setup, BEFORE any share is accepted)
 * guarantees delivery, so prompt scheduling and paused-row resume only
 * accelerate it: they must never fail a committed share, and a commit that
 * succeeded must never be reported as failed. Pure JVM code (no android
 * imports) so the boundary semantics are host-testable.
 */
public final class DurableShare {

    /** A durable or accelerated step; may throw (SQLite/WorkManager are unchecked). */
    public interface Step {
        void run() throws Exception;
    }

    private DurableShare() {
    }

    /**
     * Runs the durable commit (the outbox insert). Its failure is the only
     * "could not save": returns false so the caller rejects the share.
     */
    public static boolean commit(Step commit) {
        try {
            commit.run();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * Runs an accelerator (prompt drain, paused-row resume). A failure is
     * absorbed — the periodic drain delivers regardless (DeliverWorker calls
     * resetAuthPaused on every healthy cycle) — and must not fail the durable
     * step that already succeeded.
     */
    public static boolean accelerate(Step step) {
        try {
            step.run();
            return true;
        } catch (Exception e) {
            // Deliberate: the periodic drain delivers regardless; a committed
            // share stays saved. Surfacing this would report a false failure.
            return false;
        }
    }
}
