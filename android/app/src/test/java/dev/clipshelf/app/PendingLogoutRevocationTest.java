package dev.clipshelf.app;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

/** Pins the per-call bound so unreachable logout endpoints cannot stall the caller unbounded. */
public class PendingLogoutRevocationTest {

    @Test
    public void delayedUnreachableEndpointsBoundedPerCall() throws Exception {
        List<String> rs = new ArrayList<>(Arrays.asList("ep0", "ep1", "ep2", "ep3", "ep4"));
        List<String> attempted = new ArrayList<>();
        List<String> confirmed = new ArrayList<>();
        int attempts = PendingLogout.attemptBounded(rs, r -> {
            try {
                Thread.sleep(20); // delayed/unreachable endpoint
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            attempted.add(r);
            return false;
        }, confirmed::add, 3);
        assertEquals(3, attempts);
        assertEquals(Arrays.asList("ep0", "ep1", "ep2"), attempted);
        assertTrue(confirmed.isEmpty());
        // No logout work lost: unattempted records stay for the next call.
        assertEquals(Arrays.asList("ep0", "ep1", "ep2", "ep3", "ep4"), rs);
    }

    @Test
    public void confirmedRecordsRemovedUnattemptedKept() {
        List<String> rs = new ArrayList<>(Arrays.asList("ep0", "ep1", "ep2", "ep3", "ep4"));
        List<String> attempted = new ArrayList<>();
        List<String> confirmed = new ArrayList<>();
        int attempts = PendingLogout.attemptBounded(rs, r -> {
            attempted.add(r);
            return r.equals("ep0") || r.equals("ep2");
        }, confirmed::add, 4);
        assertEquals(4, attempts);
        assertEquals(Arrays.asList("ep0", "ep2"), confirmed);
        assertTrue(attempted.contains("ep3"));
        assertTrue(!attempted.contains("ep4"));
    }

    @Test
    public void fewerRecordsThanCapAttemptsAll() {
        List<String> rs = new ArrayList<>(Arrays.asList("ep0", "ep1"));
        List<String> attempted = new ArrayList<>();
        int attempts = PendingLogout.attemptBounded(rs, r -> {
            attempted.add(r);
            return false;
        }, r -> { }, 3);
        assertEquals(2, attempts);
        assertEquals(Arrays.asList("ep0", "ep1"), attempted);
    }

    @Test
    public void emptyListAttemptsNothing() {
        List<String> attempted = new ArrayList<>();
        List<String> confirmed = new ArrayList<>();
        int attempts = PendingLogout.attemptBounded(new ArrayList<String>(), r -> {
            attempted.add(r);
            return true;
        }, confirmed::add, 3);
        assertEquals(0, attempts);
        assertTrue(attempted.isEmpty());
        assertTrue(confirmed.isEmpty());
    }
}
