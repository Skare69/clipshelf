package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.Test;

public class PageTrackerTest {

    private PageTracker pages;

    @Before
    public void setUp() {
        pages = new PageTracker();
    }

    @Test
    public void reloadReturnsIncrementingGenerationAndRewindsOffset() {
        assertEquals("first reload returns generation 1", 1, pages.reload());
        assertEquals("second reload returns generation 2", 2, pages.reload());
        assertEquals("offset rewound to 0 after reload", 0, pages.offset());
    }

    @Test
    public void successfulCommitAdvancesOffset() {
        int gen = pages.reload();
        assertTrue("first page commit succeeds", pages.commit(gen, 0, 30));
        assertEquals("offset advances by page size", 30, pages.offset());
    }

    @Test
    public void staleGenerationCommitFailsAndKeepsOffset() {
        int stale = pages.reload();
        pages.reload();
        pages.commit(pages.generation(), 0, 30);
        assertFalse("pre-reload generation is rejected", pages.commit(stale, 0, 30));
        assertEquals("offset unchanged by stale commit", 30, pages.offset());
    }

    @Test
    public void duplicateCommitFails() {
        int gen = pages.reload();
        assertTrue(pages.commit(gen, 0, 30));
        assertFalse("double-tap duplicate commit rejected", pages.commit(gen, 0, 30));
        assertEquals("offset stays 30 after duplicate", 30, pages.offset());
    }

    @Test
    public void failedPageLeavesOffsetUntouched() {
        int gen = pages.reload();
        assertTrue(pages.commit(gen, 0, 30));
        // A failed page is modeled by NOT committing; the retry reuses offset 30.
        assertEquals("offset stays 30 when next request fails", 30, pages.offset());
        assertTrue("retry from same offset succeeds", pages.commit(gen, 30, 30));
        assertEquals(60, pages.offset());
    }

    @Test
    public void currentDistinguishesStaleFromLatest() {
        int stale = pages.reload();
        int gen = pages.reload();
        assertFalse("pre-reload generation is not current", pages.current(stale));
        assertTrue("latest generation is current", pages.current(gen));
    }
}
