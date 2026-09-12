package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import android.app.AlertDialog;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;
import dev.clipshelf.app.outbox.OutboxStore;
import dev.clipshelf.app.work.WorkScheduler;

/**
 * Hub. Re-registers the durable periodic drain on every open (KEEP is
 * idempotent) and prompts a drain, so a process kill between the local
 * outbox commit and its one-time enqueue is also healed by the next open.
 */
public class MainActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
    }

    @Override
    protected void onResume() {
        super.onResume();
        render();
        Creds.Profile profile = Creds.profile(this);
        if (profile != null && !profile.instanceId.isEmpty()) {
            WorkScheduler.ensurePeriodic(this);
            WorkScheduler.drainNow(this);
        }
    }

    private void render() {
        Creds.Profile p = Creds.profile(this);
        TextView email = findViewById(R.id.main_email);
        TextView server = findViewById(R.id.main_server);
        TextView instance = findViewById(R.id.main_instance);
        TextView summary = findViewById(R.id.main_outbox_summary);
        TextView hint = findViewById(R.id.main_hint);
        Button setup = findViewById(R.id.main_setup);
        Button outbox = findViewById(R.id.main_outbox);
        Button library = findViewById(R.id.main_library);
        Button collections = findViewById(R.id.main_collections);
        Button settings = findViewById(R.id.main_settings);
        Button logout = findViewById(R.id.main_logout);

        boolean signedIn = p != null && !p.instanceId.isEmpty();
        setup.setVisibility(signedIn ? View.GONE : View.VISIBLE);
        outbox.setVisibility(signedIn ? View.VISIBLE : View.GONE);
        library.setVisibility(signedIn ? View.VISIBLE : View.GONE);
        collections.setVisibility(signedIn ? View.VISIBLE : View.GONE);
        settings.setVisibility(signedIn ? View.VISIBLE : View.GONE);
        logout.setVisibility(signedIn ? View.VISIBLE : View.GONE);
        hint.setVisibility(signedIn ? View.VISIBLE : View.GONE);

        setup.setOnClickListener(v -> startActivity(new Intent(this, SetupActivity.class)));

        if (!signedIn) {
            email.setText(R.string.not_signed_in);
            server.setVisibility(View.GONE);
            instance.setVisibility(View.GONE);
            summary.setVisibility(View.GONE);
            return;
        }

        email.setText(getString(R.string.signed_in_as, p.email));
        server.setText(getString(R.string.server_label, p.endpoint));
        instance.setText(getString(R.string.instance_label,
                p.instanceId.length() > 8 ? p.instanceId.substring(0, 8) + "…" : p.instanceId));
        server.setVisibility(View.VISIBLE);
        instance.setVisibility(View.VISIBLE);

        OutboxStore db = new OutboxStore(this);
        int[] counts = db.countsFor(p.instanceId, p.userId);
        int others = db.countOtherIdentities(p.instanceId, p.userId);
        String text = getString(R.string.outbox_summary,
                counts[OutboxStore.STATE_QUEUED], counts[OutboxStore.STATE_PAUSED],
                counts[OutboxStore.STATE_DELIVERED]);
        if (others > 0) {
            text += "  (+" + others + ")";
        }
        summary.setText(text);
        summary.setVisibility(View.VISIBLE);

        outbox.setOnClickListener(v -> startActivity(new Intent(this, OutboxActivity.class)));
        library.setOnClickListener(v -> startActivity(new Intent(this, LibraryActivity.class)));
        collections.setOnClickListener(v -> startActivity(new Intent(this, CollectionsActivity.class)));
        settings.setOnClickListener(v -> startActivity(new Intent(this, SettingsActivity.class)));
        logout.setOnClickListener(v -> confirmLogout());
    }

    private void confirmLogout() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.logout_confirm_title)
                .setMessage(R.string.logout_confirm_message)
                .setPositiveButton(R.string.ok, (d, w) -> logout())
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void logout() {
        Creds.Profile p = Creds.profile(this);
        Creds.Session s = Creds.session(this);
        if (p != null && s != null) {
            Async.go(() -> {
                Api.logout(p.endpoint, s.token);
                return null;
            }, (r, e) -> {
                Creds.clear(this); // outbox rows stay for this identity
                render();
            });
        } else {
            Creds.clear(this);
            render();
        }
    }
}
