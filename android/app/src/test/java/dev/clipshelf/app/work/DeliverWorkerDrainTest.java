package dev.clipshelf.app.work;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import dev.clipshelf.app.work.DeliverWorker.Deliver;
import dev.clipshelf.app.work.DeliverWorker.Queue;

/**
 * Pins drain(): immediate continuation on clean cap stop; retry reserved for
 * transient; request IDs stable across continuations.
 */
public class DeliverWorkerDrainTest {

    // mirror of DeliverWorker.OUTCOME_* (values 0,1,2,3)
    private static final int OUTCOME_CONTINUE = 0;
    private static final int OUTCOME_STOP_TRANSIENT = 1;
    private static final int OUTCOME_STOP_AUTH = 2;
    private static final int OUTCOME_STOP_MISMATCH = 3;

    /** ArrayList-backed queue; deliver removes the row and returns its outcome. */
    private static final class Harness {
        final List<String> queued = new ArrayList<>();
        final List<String> attempted = new ArrayList<>();
        final Map<String, Integer> outcomes = new LinkedHashMap<>();

        Queue<String> queue() {
            return (instanceId, userId, limit) ->
                    new ArrayList<>(queued.subList(0, Math.min(limit, queued.size())));
        }

        Deliver<String> deliver() {
            return row -> {
                int outcome = outcomes.getOrDefault(row, OUTCOME_CONTINUE);
                queued.remove(row);
                attempted.add(row);
                if (outcome == OUTCOME_STOP_AUTH) {
                    queued.clear(); // auth pause empties the identity's queue
                }
                return outcome;
            };
        }
    }

    private static Harness of(int n) {
        Harness h = new Harness();
        for (int i = 1; i <= n; i++) {
            h.queued.add("row-" + i);
        }
        return h;
    }

    @Test
    public void cleanFiftyOfSixtyChainsInsteadOfBackoff() {
        Harness h = of(60);
        assertEquals(DeliverWorker.DRAIN_BACKLOG,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(50, h.attempted.size());
        for (int i = 0; i < 50; i++) {
            assertEquals("row-" + (i + 1), h.attempted.get(i));
        }
    }

    @Test
    public void continuationPreservesRequestIds() {
        Harness h = of(60);
        assertEquals(DeliverWorker.DRAIN_BACKLOG,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(DeliverWorker.DRAIN_DONE,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(60, h.attempted.size());
        for (int i = 0; i < 60; i++) {
            assertEquals("row-" + (i + 1), h.attempted.get(i)); // each ID exactly once
        }
    }

    @Test
    public void transientAtTheCapRetries() {
        Harness h = of(60);
        h.outcomes.put("row-50", OUTCOME_STOP_TRANSIENT);
        assertEquals(DeliverWorker.DRAIN_RETRY,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(50, h.attempted.size());
    }

    @Test
    public void earlyTransientRetries() {
        Harness h = of(55);
        h.outcomes.put("row-3", OUTCOME_STOP_TRANSIENT);
        assertEquals(DeliverWorker.DRAIN_RETRY,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(3, h.attempted.size());
    }

    @Test
    public void exactlyFiftyAllDeliveredCompletes() {
        Harness h = of(50);
        assertEquals(DeliverWorker.DRAIN_DONE,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
        assertEquals(50, h.attempted.size());
    }

    @Test
    public void mismatchStopWithRemainingRowsRetries() {
        Harness h = of(12);
        h.outcomes.put("row-5", OUTCOME_STOP_MISMATCH);
        assertEquals(DeliverWorker.DRAIN_RETRY,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
    }

    @Test
    public void authStopWithEmptiedQueueCompletes() {
        Harness h = of(12);
        h.outcomes.put("row-5", OUTCOME_STOP_AUTH);
        assertEquals(DeliverWorker.DRAIN_DONE,
                DeliverWorker.drain(h.queue(), "i", "u", h.deliver()));
    }
}
