package dev.clipshelf.app.outbox;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

/**
 * EXTRA_TEXT extraction at the external share boundary. Share sheets deliver
 * EXTRA_TEXT as either String or a non-String CharSequence (e.g. Spannable);
 * the boundary must accept both and refuse non-text extras instead of
 * rejecting a valid share as empty.
 */
public class ShareBoundaryTest {

    @Test
    public void plainStringExtraIsAccepted() {
        assertEquals("https://example.com/a", OutboxPolicy.sharedText("https://example.com/a"));
    }

    @Test
    public void charSequenceExtraIsAccepted() {
        CharSequence styled = new StringBuilder("https://example.com/b");
        assertEquals("https://example.com/b", OutboxPolicy.sharedText(styled));
    }

    @Test
    public void missingExtraIsRejected() {
        assertNull(OutboxPolicy.sharedText(null));
    }

    @Test
    public void nonTextExtraIsRejected() {
        assertNull(OutboxPolicy.sharedText(12345));
    }
}
