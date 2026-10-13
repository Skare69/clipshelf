package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import com.google.android.material.chip.Chip;
import com.google.android.material.chip.ChipGroup;

import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;

import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;

/**
 * The selected category chip must not survive into a load that has no such
 * category: a stale cat filter empties the list behind an unchecked "All"
 * chip, so a non-empty collection looks empty (v0.11.0 review finding).
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class LibraryChipResetTest {

    private LibraryActivity activity;
    private List<Api.Entry> entries;
    private ChipGroup cats;

    @Before
    public void setUp() throws Exception {
        // No seeded session: onCreate stops after inflating the views, which is
        // all the chip logic needs. No network path runs in this test.
        activity = Robolectric.buildActivity(LibraryActivity.class).setup().get();
        entries = (List<Api.Entry>) field("entries").get(activity);
        field("adapter").set(activity, adapter(activity));
        cats = activity.findViewById(R.id.library_cats);
    }

    /** The same calls the loadMore() result callback makes after each page. */
    private void load(List<Api.Entry> page) throws Exception {
        entries.clear();
        entries.addAll(page);
        method("rebuildChips").invoke(activity);
        method("applyFilter").invoke(activity);
    }

    private Chip chip(String text) {
        for (int i = 0; i < cats.getChildCount(); i++) {
            Chip c = (Chip) cats.getChildAt(i);
            if (text.equals(c.getText().toString())) {
                return c;
            }
        }
        throw new AssertionError("no chip '" + text + "'");
    }

    @SuppressWarnings("unchecked")
    private List<Api.Entry> visible() throws Exception {
        return (List<Api.Entry>) field("visible").get(activity);
    }

    private static Field field(String name) throws Exception {
        Field f = LibraryActivity.class.getDeclaredField(name);
        f.setAccessible(true);
        return f;
    }

    private static Method method(String name) throws Exception {
        Method m = LibraryActivity.class.getDeclaredMethod(name);
        m.setAccessible(true);
        return m;
    }

    private static Object adapter(LibraryActivity act) throws Exception {
        Class<?> cls = Class.forName(LibraryActivity.class.getName() + "$Adapter");
        var ctor = cls.getDeclaredConstructor(LibraryActivity.class);
        ctor.setAccessible(true);
        return ctor.newInstance(act);
    }

    /** Api.Entry(JSONObject) is package-private (net); the test builds labels only. */
    private static Api.Entry entry(String id, String cat) {
        try {
            var ctor = Api.Entry.class.getDeclaredConstructor(JSONObject.class);
            ctor.setAccessible(true);
            return ctor.newInstance(new JSONObject(
                    "{\"id\":\"" + id + "\",\"kind\":\"link\",\"cat\":[\"" + cat + "\"]}"));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Chip A selected, then a collection with only B: everything visible, All checked. */
    @Test
    public void staleCategoryFallsBackToAllOnNewLoad() throws Exception {
        load(java.util.Arrays.asList(entry("e1", "A"), entry("e2", "A")));

        chip("A (2)").performClick(); // the user picks category A
        assertEquals(2, visible().size());
        assertTrue(chip("A (2)").isChecked());

        load(java.util.Arrays.asList(entry("e3", "B"), entry("e4", "B")));

        assertEquals("a stale category filter must not hide any entry",
                2, visible().size());
        assertTrue("All must be checked again", chip("All (2)").isChecked());
        assertFalse(chip("B (2)").isChecked());
    }

    /** A category that still exists after the reload keeps its selection. */
    @Test
    public void liveCategoryKeepsSelectionOnNewLoad() throws Exception {
        load(java.util.Arrays.asList(entry("e1", "A"), entry("e2", "B")));

        chip("A (1)").performClick();
        assertEquals(1, visible().size());

        load(java.util.Arrays.asList(entry("e3", "A"), entry("e4", "B")));

        assertEquals(1, visible().size());
        assertEquals("A", visible().get(0).catLabel());
        assertTrue(chip("A (1)").isChecked());
    }
}
