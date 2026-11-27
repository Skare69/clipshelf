package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.app.Application;
import android.content.ActivityNotFoundException;
import android.content.ContextWrapper;
import android.content.Intent;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.annotation.Config;

/**
 * The server can hand the app non-http(s) entry URLs (tel:, intent:,
 * javascript:, sms:, file:); only http(s) may arm an ACTION_VIEW intent,
 * and a missing handler must never crash the entry screen.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class EntryOpenGuardTest {

    /** Refuses every scheme outside http(s), plus null, empty and schemeless. */
    @Test
    public void isEntryUrlRefusesNonHttpSchemes() {
        assertFalse(Ui.isEntryUrl("tel:+15551234"));
        assertFalse(Ui.isEntryUrl("intent://x/"));
        assertFalse(Ui.isEntryUrl("javascript:alert(1)"));
        assertFalse(Ui.isEntryUrl("sms:+15551234"));
        assertFalse(Ui.isEntryUrl("file:///data/data/x"));
        assertFalse(Ui.isEntryUrl(""));
        assertFalse(Ui.isEntryUrl(null));
        assertFalse(Ui.isEntryUrl("example.com/a"));
    }

    /** Allows http and https in any letter case. */
    @Test
    public void isEntryUrlAllowsHttpAndHttpsInAnyCase() {
        assertTrue(Ui.isEntryUrl("https://example.com/a"));
        assertTrue(Ui.isEntryUrl("http://example.com/a"));
        assertTrue(Ui.isEntryUrl("HTTPS://EXAMPLE.COM/A"));
    }

    /** A refused scheme never arms an intent at all. */
    @Test
    public void openEntryIgnoresNonHttpUrl() {
        Application app = RuntimeEnvironment.getApplication();
        Ui.openEntry(app, "tel:+15551234");
        assertNull(Shadows.shadowOf(app).getNextStartedActivity());
    }

    /** An http(s) entry URL opens as ACTION_VIEW with the URL as its data. */
    @Test
    public void openEntryStartsActionViewWithUrl() {
        androidx.appcompat.app.AppCompatActivity act =
                Robolectric.buildActivity(androidx.appcompat.app.AppCompatActivity.class).setup().get();
        Ui.openEntry(act, "https://example.com/a");
        Intent started = Shadows.shadowOf(act).getNextStartedActivity();
        assertNotNull("no activity started for https URL", started);
        assertEquals(Intent.ACTION_VIEW, started.getAction());
        assertEquals("https://example.com/a", started.getData().toString());
    }

    /** A device with no http(s) handler throws ActivityNotFoundException
     *  out of startActivity; openEntry must swallow it, not crash the
     *  entry screen. */
    @Test
    public void openEntrySwallowsMissingHandler() {
        androidx.appcompat.app.AppCompatActivity act =
                Robolectric.buildActivity(androidx.appcompat.app.AppCompatActivity.class).setup().get();
        ContextWrapper noHandler = new ContextWrapper(act) {
            @Override
            public void startActivity(Intent intent) {
                throw new ActivityNotFoundException("no handler for " + intent);
            }
        };
        Ui.openEntry(noHandler, "https://example.com/a");
        assertNull(Shadows.shadowOf(act).getNextStartedActivity());
    }
}
