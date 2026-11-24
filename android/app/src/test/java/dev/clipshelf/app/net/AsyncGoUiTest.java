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

import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BiConsumer;
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
 * A drop is silent, so absence alone proves nothing: the result may simply
 * not have dispatched yet. destroyedActivityDropsTheOutcome therefore
 * arranges proof that the destroyed result's runnable has already executed
 * before asserting the drop: the pool is saturated with gated blockers that
 * wait without a timeout, so the destroyed job and the live probe run
 * sequentially on one worker and their outcomes enter the main looper queue
 * in FIFO order. Once the live probe's outcome delivers, the destroyed
 * runnable has been executed too — and must not have called done. The
 * finally block releases every gate so no worker stays blocked into another
 * test.
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
        ActivityController<Activity> controller =
                Robolectric.buildActivity(Activity.class).setup();
        controller.destroy();
        Activity live = Robolectric.buildActivity(Activity.class).setup().get();
        // Robolectric's main looper only runs tasks on explicit idle(), so the
        // outcomes below queue up untouched until the drain at the bottom.
        // Saturate the fixed pool so the jobs below queue in FIFO order
        // and later run sequentially on one worker, in release order.
        Gate[] blockers = new Gate[4];
        Gate destroyedJob = null;
        Gate liveJob = null;
        try {
            for (int i = 0; i < blockers.length; i++) {
                blockers[i] = Gate.submit();
            }
            for (Gate blocker : blockers) {
                await(blocker.started, "the blocker jobs never started");
            }

            AtomicBoolean delivered = new AtomicBoolean(false);
            destroyedJob = Gate.via(controller.get(), () -> "x",
                    (r, e) -> delivered.set(true));
            AtomicBoolean liveGot = new AtomicBoolean(false);
            liveJob = Gate.via(live, () -> "x",
                    (r, e) -> liveGot.set(true));

            blockers[0].release();
            await(destroyedJob.started, "the destroyed-path job never started");
            destroyedJob.release();
            await(liveJob.started, "the live probe never started");
            liveJob.release();

            // Drain until the probe delivers. FIFO order on one worker means
            // the destroyed outcome's runnable already ran in this drain.
            await(liveGot::get, "dispatch does not occur: the probe never delivered");
            assertFalse("the destroyed activity must drop the outcome once dispatched",
                    delivered.get());
        } finally {
            // Every gate waits without a timeout, so nothing frees itself:
            // release them all here so no pool worker stays blocked into
            // another test.
            if (destroyedJob != null) {
                destroyedJob.release();
            }
            if (liveJob != null) {
                liveJob.release();
            }
            for (Gate blocker : blockers) {
                if (blocker != null) {
                    blocker.release();
                }
            }
        }
    }

    /** A gated job: the Callable waits at started, the test opens release. */
    private static final class Gate {
        private final CountDownLatch started = new CountDownLatch(1);
        private final CountDownLatch release = new CountDownLatch(1);

        /** Occupies a pool worker until released; the outcome is discarded. */
        private static Gate submit() {
            Gate gate = new Gate();
            Async.go(() -> {
                gate.started.countDown();
                gate.release.await();
                return null;
            }, (r, e) -> { });
            return gate;
        }

        /** Queues a gated goUi job: the outcome flows through goUi's guard. */
        private static Gate via(Activity activity, Callable<Object> job,
                BiConsumer<Object, Exception> done) {
            Gate gate = new Gate();
            Async.goUi(activity, () -> {
                gate.started.countDown();
                gate.release.await();
                return job.call();
            }, done);
            return gate;
        }

        private void release() {
            release.countDown();
        }
    }

    private static void await(CountDownLatch latch, String message) throws Exception {
        assertTrue(message, latch.await(5, TimeUnit.SECONDS));
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
