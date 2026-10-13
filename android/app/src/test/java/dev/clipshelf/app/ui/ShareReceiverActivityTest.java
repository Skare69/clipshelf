package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.Intent;
import android.widget.TextView;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import java.io.File;
import java.util.Arrays;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.outbox.OutboxPolicy;
import dev.clipshelf.app.outbox.OutboxStore;

/**
 * The share boundary must never claim "saved" unless the row is durably in
 * the outbox database (defect 1 contract, persistence failures included).
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class ShareReceiverActivityTest {

    private Context ctx;

    @Before
    public void setUp() {
        ctx = RuntimeEnvironment.getApplication();
        // Profile is written via the real prefs keys; the token stays Keystore-free here.
        ctx.getSharedPreferences("clipshelf", Context.MODE_PRIVATE)
                .edit()
                .putString("endpoint", "https://e.example")
                .putString("instance_id", "inst-1")
                .putString("user_id", "user-1")
                .putString("email", "a@b.c")
                .commit();
        // No WorkManager init: DurableShare.accelerate absorbs the enqueue failure,
        // so the saved confirmation stays correct without leaking executor threads
        // that would keep the shared test JVM alive after the run.
    }

    private static Intent shareIntent(String text) {
        Intent intent = new Intent(Intent.ACTION_SEND);
        intent.setType("text/plain");
        intent.putExtra(Intent.EXTRA_TEXT, text);
        return intent;
    }

    /** A valid share is persisted and confirmed as saved on the phone. */
    @Test
    public void validShareIsPersistedAndConfirmed() {
        ShareReceiverActivity activity = Robolectric.buildActivity(
                ShareReceiverActivity.class, shareIntent("look https://e.io/1")).setup().get();

        assertEquals(ctx.getString(R.string.share_saved),
                ((TextView) activity.findViewById(R.id.share_title)).getText());

        OutboxStore db = new OutboxStore(ctx);
        assertEquals(1, db.listNewest(10).size());
        assertEquals("inst-1", db.listNewest(10).get(0).instanceId);
    }

    /** A persistence failure shows the storage-error rejection, never the saved confirmation. */
    @Test
    public void persistenceFailureIsRejectedNotConfirmed() {
        // Occupy the database path with a directory so opening it must fail.
        File dbPath = ctx.getDatabasePath("outbox.db");
        assertTrue(dbPath.getParentFile().isDirectory() || dbPath.getParentFile().mkdirs());
        assertTrue(dbPath.mkdir());

        ShareReceiverActivity activity = Robolectric.buildActivity(
                ShareReceiverActivity.class, shareIntent("look https://e.io/1")).setup().get();

        assertEquals(ctx.getString(R.string.share_rejected_title),
                ((TextView) activity.findViewById(R.id.share_title)).getText());
        assertEquals(ctx.getString(R.string.share_storage_error),
                ((TextView) activity.findViewById(R.id.share_detail)).getText());
    }

    /** Each refused share shows its reason and leaves the outbox empty. */
    @Test
    public void refusedSharesShowTheirReason() {
        char[] big = new char[OutboxPolicy.MAX_TEXT_BYTES + 1];
        Arrays.fill(big, 'x');
        StringBuilder links = new StringBuilder();
        for (int i = 0; i <= OutboxPolicy.MAX_URLS; i++) {
            links.append("https://e.io/").append(i).append(' ');
        }
        Object[][] cases = {
                {"   ", R.string.share_empty},
                {new String(big), R.string.share_too_large},
                {"just some words", R.string.share_no_url},
                {links.toString(), R.string.share_too_many_urls},
        };
        for (Object[] c : cases) {
            ShareReceiverActivity activity = Robolectric.buildActivity(
                    ShareReceiverActivity.class, shareIntent((String) c[0])).setup().get();
            assertEquals(ctx.getString((Integer) c[1]),
                    ((TextView) activity.findViewById(R.id.share_detail)).getText());
        }
        assertEquals(0, new OutboxStore(ctx).listNewest(10).size());
    }
}
