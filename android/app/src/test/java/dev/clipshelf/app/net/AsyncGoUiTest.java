package dev.clipshelf.app.net;

import android.app.Activity;
import android.os.Looper;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.Shadows;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;

import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

/**
 * goUi's contract: the outcome reaches a live activity, and a destroyed
 * activity drops it. The library tests destroy abandoned activities in
 * tearDown on exactly this guarantee: a late outcome from a dead endpoint
 * must never fire a Toast in the next test's Robolectric environment (CI
 * NPE in ShadowToast.show, 2026-11-23).
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class AsyncGoUiTest {

    @Test
    public void liveActivityGetsTheOutcome() throws Exception {
        Activity activity = Robolectric.buildActivity(Activity.class).setup().get();
        AtomicBoolean ran = new AtomicBoolean(false);
        Async.goUi(activity, () -> "x", (r, e) -> ran.set(true));
        await(ran::get);
        assertTrue("the live activity must receive the outcome", ran.get());
    }

    @Test
    public void destroyedActivityDropsTheOutcome() throws Exception {
        ActivityController<Activity> controller =
                Robolectric.buildActivity(Activity.class).setup();
        controller.destroy();
        AtomicBoolean ran = new AtomicBoolean(false);
        Async.goUi(controller.get(), () -> "x", (r, e) -> ran.set(true));
        // Give the pool thread ample time to deliver; the drop must hold for
        // the whole window, so a late post cannot leak into the next test.
        long deadline = System.currentTimeMillis() + 1_000;
        while (System.currentTimeMillis() < deadline && !ran.get()) {
            Shadows.shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(10);
        }
        assertFalse("the destroyed activity must drop the outcome", ran.get());
    }

    /** Idles the main looper until check holds or the 5s bound expires. */
    private static void await(BooleanSupplier check) throws Exception {
        long deadline = System.currentTimeMillis() + 5_000;
        while (!check.getAsBoolean()) {
            if (System.currentTimeMillis() > deadline) {
                return;
            }
            Shadows.shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(10);
        }
    }
}
