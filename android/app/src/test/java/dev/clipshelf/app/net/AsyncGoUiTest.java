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

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

/**
 * goUi's contract: the outcome reaches a live activity, and a destroyed
 * activity drops it. The library tests destroy abandoned activities in
 * tearDown on exactly this guarantee: a late outcome from a dead endpoint
 * must never fire a Toast in the next test's Robolectric environment (CI
 * NPE in ShadowToast.show, 2026-11-23).
 *
 * The drop assertion needs dispatch evidence first: a probe outcome to a
 * live activity must land before the destroyed activity is judged, so a
 * slow executor fails the probe instead of passing an empty negative.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class AsyncGoUiTest {

    @Test
    public void liveActivityGetsTheOutcome() throws Exception {
        Activity activity = Robolectric.buildActivity(Activity.class).setup().get();
        AtomicBoolean ran = new AtomicBoolean(false);
        Async.goUi(activity, () -> "x", (r, e) -> ran.set(true));
        await(ran::get, "the live activity must receive the outcome");
    }

    @Test
    public void destroyedActivityDropsTheOutcome() throws Exception {
        Activity live = Robolectric.buildActivity(Activity.class).setup().get();
        ActivityController<Activity> controller =
                Robolectric.buildActivity(Activity.class).setup();
        controller.destroy();

        // Probe: the same executor and looper must dispatch an outcome to a
        // live activity, else this environment cannot judge the drop at all.
        AtomicReference<String> liveGot = new AtomicReference<>(null);
        CountDownLatch probeStarted = new CountDownLatch(1);
        CountDownLatch probeRelease = new CountDownLatch(1);
        Async.goUi(live, () -> {
            probeStarted.countDown();
            probeRelease.await(5, TimeUnit.SECONDS);
            return "x";
        }, (r, e) -> liveGot.set(r));
        assertTrue("the probe job never started", probeStarted.await(5, TimeUnit.SECONDS));
        probeRelease.countDown();
        await(() -> liveGot.get() != null,
                "dispatch does not occur: the probe outcome never landed");

        // Now the destroyed activity, gated the same way so its post is
        // already due when the window opens.
        AtomicBoolean destroyedGot = new AtomicBoolean(false);
        CountDownLatch droppedStarted = new CountDownLatch(1);
        CountDownLatch droppedRelease = new CountDownLatch(1);
        Async.goUi(controller.get(), () -> {
            droppedStarted.countDown();
            droppedRelease.await(5, TimeUnit.SECONDS);
            return "x";
        }, (r, e) -> destroyedGot.set(true));
        assertTrue("the drop-path job never started", droppedStarted.await(5, TimeUnit.SECONDS));
        droppedRelease.countDown();

        // Hold a window that dwarfs the just-proven dispatch latency; the
        // guard must keep the outcome dropped for the whole window.
        long deadline = System.currentTimeMillis() + 2_000;
        while (System.currentTimeMillis() < deadline && !destroyedGot.get()) {
            Shadows.shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(10);
        }
        assertFalse("the destroyed activity must drop the outcome", destroyedGot.get());
    }

    /** Idles the main looper until check holds; fails with message at the 5s bound. */
    private static void await(BooleanSupplier check, String message) throws Exception {
        long deadline = System.currentTimeMillis() + 5_000;
        while (!check.getAsBoolean()) {
            if (System.currentTimeMillis() > deadline) {
                throw new AssertionError(message);
            }
            Shadows.shadowOf(Looper.getMainLooper()).idle();
            Thread.sleep(10);
        }
    }
}
