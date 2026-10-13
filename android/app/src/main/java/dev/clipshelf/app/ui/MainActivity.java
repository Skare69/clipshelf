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
            startActivity(new Intent(this, LibraryActivity.class));
        } else {
            startActivity(new Intent(this, SetupActivity.class));
        }
        finish();
    }
}
