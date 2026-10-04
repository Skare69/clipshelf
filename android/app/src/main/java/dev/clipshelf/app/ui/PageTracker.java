package dev.clipshelf.app.ui;

/**
 * Tracks the active paged query for {@link LibraryActivity}: a monotonic
 * generation guards against stale async callbacks (a response issued before a
 * reload/filter change must be dropped), and the expected offset guards
 * against duplicate or failed pages. A page is appended only when
 * {@link #commit} succeeds, so {@code offset} advances only after real success
 * and a double-tap cannot append the same page twice.
 */
final class PageTracker {
    private int generation;
    private int offset;

    /** New result set: drop in-flight pages and rewind offset. */
    int reload() {
        offset = 0;
        return ++generation;
    }

    int generation() {
        return generation;
    }

    boolean current(int requestGeneration) {
        return requestGeneration == generation;
    }

    int offset() {
        return offset;
    }

    /** First matching commit wins, so a double-tap cannot append twice. False for stale or duplicate pages. */
    boolean commit(int requestGeneration, int fromOffset, int pageItems) {
        if (requestGeneration != generation || fromOffset != offset) return false;
        offset = fromOffset + pageItems;
        return true;
    }
}
