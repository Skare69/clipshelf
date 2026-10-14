package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;

import android.view.View;
import android.widget.TextView;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;

import dev.clipshelf.app.R;

/**
 * The library prompt card must hide the whole From row (label included)
 * when the entry has no host — never show a bare "From" label.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class EntryFromRowTest {

    private View inflateAndBind(String url) {
        androidx.appcompat.app.AppCompatActivity act =
                Robolectric.buildActivity(androidx.appcompat.app.AppCompatActivity.class).setup().get();
        act.setTheme(R.style.Theme_Clipshelf);
        View row = act.getLayoutInflater().inflate(R.layout.item_entry, null);
        LibraryActivity.bindFrom(row, url);
        return row;
    }

    /** A prompt (no url) hides the whole From row, label included. */
    @Test
    public void promptWithoutHostHidesWholeFromRow() {
        View row = inflateAndBind("");
        assertEquals(View.GONE, row.findViewById(R.id.entry_from_row).getVisibility());
    }

    /** A link with a host shows the row and the stripped host. */
    @Test
    public void linkWithHostShowsRowAndHost() {
        View row = inflateAndBind("https://www.example.com/x");
        assertEquals(View.VISIBLE, row.findViewById(R.id.entry_from_row).getVisibility());
        TextView from = row.findViewById(R.id.entry_from);
        assertEquals("example.com", from.getText().toString());
    }
}
