package dev.clipshelf.app.net;

import android.os.Handler;
import android.os.Looper;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Tiny off-main-thread helper: run a job on a pool, deliver the outcome on the UI thread. */
public final class Async {

    public interface Job<T> {
        T run() throws Exception;
    }

    public interface Done<T> {
        void done(T result, Exception error);
    }

    private static final ExecutorService POOL = Executors.newFixedThreadPool(4);
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private Async() {
    }

    public static <T> void go(Job<T> job, Done<T> done) {
        POOL.execute(() -> {
            T result = null;
            Exception error = null;
            try {
                result = job.run();
            } catch (Exception e) {
                error = e;
            }
            final T r = result;
            final Exception e = error;
            MAIN.post(() -> done.done(r, e));
        });
    }
}
