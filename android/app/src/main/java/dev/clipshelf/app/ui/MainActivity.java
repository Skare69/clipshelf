package dev.clipshelf.app.ui;

import android.content.Intent;
import android.os.Bundle;

import androidx.appcompat.app.AppCompatActivity;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.PendingLogout;
import dev.clipshelf.app.net.Async;
import dev.clipshelf.app.work.WorkScheduler;

/**
 * Entry router. Re-registers the durable periodic drain on every open (KEEP
 * is idempotent) and prompts a drain, so a process kill between the local
 * outbox commit and its one-time enqueue is also healed by the next open.
 * Signed in routes straight to Library; otherwise to setup.
 */
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (!PendingLogout.pendingLabels(this).isEmpty()) {
            // This router finishes at once; the revocation outlives it.
            android.content.Context app = getApplicationContext();
            Async.go(() -> {
                PendingLogout.revokeAll(app);
                return null;
            }, (r, e) -> { });
        }
        Creds.Profile profile = Creds.profile(this);
        boolean signedIn = profile != null && !profile.instanceId.isEmpty();
        if (signedIn) {
            WorkScheduler.ensurePeriodic(this);
            WorkScheduler.drainNow(this);
            startActivity(route(LibraryActivity.class));
        } else {
            startActivity(route(SetupActivity.class));
        }
        finish();
    }

    /**
     * The router finishes at once, so with launchMode="singleTask" a relaunch
     * (launcher icon, share-sheet "Open") finds no live MainActivity to clear
     * to and stacks a fresh one on top of the old task. CLEAR_TOP|SINGLE_TOP
     * makes the target reuse the running instance, so Back still exits at the
     * root and the Library keeps its place.
     */
    private Intent route(Class<?> cls) {
        return new Intent(this, cls)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
    }
}
