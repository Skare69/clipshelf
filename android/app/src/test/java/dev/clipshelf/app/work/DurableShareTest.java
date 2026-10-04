package dev.clipshelf.app.work;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

/**
 * Durable-step error boundaries. A prompt-scheduling failure after a durable
 * commit is an accelerator failure only: it must be absorbed (the periodic
 * drain delivers) and never report a committed share as "could not save".
 * Only the commit itself may fail the save.
 */
public class DurableShareTest {

    @Test
    public void commitFailureFailsTheShare() {
        assertFalse(DurableShare.commit(() -> { throw new IllegalStateException("db down"); }));
    }

    @Test
    public void commitSuccessSucceedsTheShare() {
        final boolean[] ran = {false};
        assertTrue(DurableShare.commit(() -> ran[0] = true));
        assertTrue(ran[0]);
    }

    @Test
    public void acceleratorFailureIsAbsorbedAndNotASaveFailure() {
        boolean saved = DurableShare.commit(() -> { });
        assertTrue(saved); // committed before the scheduling step below failed
        assertFalse(DurableShare.accelerate(() -> { throw new IllegalStateException("workmanager down"); }));
    }

    @Test
    public void acceleratorSuccessReportsRan() {
        final boolean[] ran = {false};
        assertTrue(DurableShare.accelerate(() -> ran[0] = true));
        assertTrue(ran[0]);
    }
}
