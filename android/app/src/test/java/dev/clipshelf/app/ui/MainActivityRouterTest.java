package dev.clipshelf.app.ui;

import static android.content.Intent.FLAG_ACTIVITY_CLEAR_TOP;
import static android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Application;
import android.content.Context;
import android.content.Intent;

import androidx.work.testing.WorkManagerTestInitHelper;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.annotation.Config;

/**
 * The router finishes itself, so with launchMode="singleTask" a relaunch
 * (launcher icon, share-sheet "Open") has no live MainActivity to clear to and
 * stacks a fresh one on top of the old task. The intents it starts must carry
 * CLEAR_TOP|SINGLE_TOP, so the target reuses the running Library/Setup instead
 * of stacking a second one (regression: relaunch from Settings stacked a second
 * Library and Back stopped exiting).
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class MainActivityRouterTest {

    private static final int ROUTE_FLAGS = FLAG_ACTIVITY_CLEAR_TOP | FLAG_ACTIVITY_SINGLE_TOP;

    private Context ctx;

    @Before
    public void setUp() {
        ctx = RuntimeEnvironment.getApplication();
        // The signed-in branch enqueues WorkManager work; the test initializer
        // absorbs it without touching the network.
        WorkManagerTestInitHelper.initializeTestWorkManager(ctx);
    }

    private void writeSignedIn(boolean signedIn) {
        if (!signedIn) {
            return;
        }
        // Profile is written via the real prefs keys; the token stays Keystore-free here.
        ctx.getSharedPreferences("clipshelf", Context.MODE_PRIVATE)
                .edit()
                .putString("endpoint", "https://e.example")
                .putString("instance_id", "inst-1")
                .putString("user_id", "user-1")
                .putString("email", "a@b.c")
                .commit();
    }

    private void assertRoutedTo(Class<?> target) {
        MainActivity router = Robolectric.buildActivity(MainActivity.class).setup().get();
        Intent routed = Shadows.shadowOf((Application) ctx).getNextStartedActivity();
        assertNotNull("router started no activity", routed);
        assertEquals(target.getName(), routed.getComponent().getClassName());
        assertEquals(ROUTE_FLAGS, routed.getFlags() & ROUTE_FLAGS);
        assertTrue("router must finish at once", router.isFinishing());
    }

    /** Signed in: the router reuses the existing Library instead of stacking one. */
    @Test
    public void signedInRoutesToLibraryWithClearTopSingleTop() {
        writeSignedIn(true);
        assertRoutedTo(LibraryActivity.class);
    }

    /** Signed out: the same reuse contract holds for the setup screen. */
    @Test
    public void signedOutRoutesToSetupWithClearTopSingleTop() {
        writeSignedIn(false);
        assertRoutedTo(SetupActivity.class);
    }
}
