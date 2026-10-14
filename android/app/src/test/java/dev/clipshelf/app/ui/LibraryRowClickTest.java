package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;

import android.content.Context;
import android.content.Intent;
import android.widget.ListAdapter;
import android.widget.ListView;

import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.annotation.Config;
import org.robolectric.annotation.ConscryptMode;
import org.robolectric.shadows.ShadowActivity;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.security.Provider;
import java.security.Security;
import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.PendingLogoutRetryTest;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;

/**
 * Regression (v0.11.0 review): the always-visible entry_details button marks
 * every label row explicitly focusable, so AbsListView skips its item click
 * and a tap on the card does nothing. The row itself must open the entry,
 * as in v0.10.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
@ConscryptMode(ConscryptMode.Mode.OFF)
public class LibraryRowClickTest {

    private static final String PROVIDER = "FakeAndroidKeyStore";

    @Before
    public void setUp() throws Exception {
        // Robolectric has no AndroidKeyStore; the fixed-key stand-in lets
        // Creds.save/session round-trip the token through the real AES-GCM path.
        Provider p = new Provider(PROVIDER, 1.0, "test-only AndroidKeyStore") {
            {
                putService(new Service(this, "KeyStore", "AndroidKeyStore",
                        PendingLogoutRetryTest.FixedKeyStore.class.getName(), null, null) {
                    @Override
                    public Object newInstance(Object param) {
                        return new PendingLogoutRetryTest.FixedKeyStore();
                    }
                });
            }
        };
        Security.addProvider(p);
        Context ctx = RuntimeEnvironment.getApplication();
        // Dead endpoint: onCreate's Api.me call fails on the pool thread and
        // its outcome is posted to a looper this test never idles.
        Creds.save(ctx, new Creds.Profile("http://127.0.0.1:9/", "i", "u", "a@b.c", null, null),
                "tok");
    }

    @After
    public void tearDown() throws Exception {
        Security.removeProvider(PROVIDER);
    }

    /** A tap on the label row (outside the buttons) starts EntryActivity. */
    @Test
    public void tappingTheRowOpensTheEntry() throws Exception {
        LibraryActivity activity =
                Robolectric.buildActivity(LibraryActivity.class).setup().get();
        ListView list = activity.findViewById(R.id.library_list);
        ListAdapter adapter = list.getAdapter();

        // One entry, injected straight into the backing list: row binding is
        // what is under test, not the network page load.
        rawList(activity, "entries").add(entry("e1"));
        Method applyFilter = LibraryActivity.class.getDeclaredMethod("applyFilter");
        applyFilter.setAccessible(true);
        applyFilter.invoke(activity);
        assertEquals(1, adapter.getCount());

        adapter.getView(0, null, list).performClick();

        ShadowActivity shadow = Shadows.shadowOf(activity);
        Intent started = shadow.getNextStartedActivity();
        assertNotNull("row tap must start EntryActivity", started);
        assertEquals(EntryActivity.class.getName(), started.getComponent().getClassName());
        assertEquals("e1", started.getStringExtra("entryId"));
    }

    private static Api.Entry entry(String id) throws Exception {
        JSONObject json = new JSONObject("{\"id\":\"" + id + "\",\"kind\":\"link\","
                + "\"url\":\"https://e.io/1\",\"title\":\"T\"}");
        Constructor<Api.Entry> ctor = Api.Entry.class.getDeclaredConstructor(JSONObject.class);
        ctor.setAccessible(true);
        return ctor.newInstance(json);
    }

    @SuppressWarnings("unchecked")
    private static List<Api.Entry> rawList(LibraryActivity activity, String name) throws Exception {
        Field f = LibraryActivity.class.getDeclaredField(name);
        f.setAccessible(true);
        return (List<Api.Entry>) f.get(activity);
    }
}
