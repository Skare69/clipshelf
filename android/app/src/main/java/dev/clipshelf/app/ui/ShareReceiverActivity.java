package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.work.WorkScheduler;
import dev.clipshelf.app.outbox.OutboxPolicy;
import dev.clipshelf.app.outbox.OutboxStore;

/**
 * External share boundary. ACTION_SEND text/plain only. Validates MIME,
 * content and byte bounds at this trust boundary (the server re-validates),
 * commits the share durably under the current instance/account/destination,
 * enqueues prompt delivery, confirms "saved on phone", and returns to the
 * source app. No network access happens here.
 */
public class ShareReceiverActivity extends Activity {

    private static final long CONFIRM_VISIBLE_MS = 1500L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_share);

        Intent intent = getIntent();
        TextView title = findViewById(R.id.share_title);
        TextView detail = findViewById(R.id.share_detail);
        TextView destination = findViewById(R.id.share_destination);
        Button open = findViewById(R.id.share_open);

        String type = intent == null ? null : intent.getType();
        String text = intent == null ? null : intent.getStringExtra(Intent.EXTRA_TEXT);

        // Refuse everything but the supported share shape before any persistence.
        if (!"text/plain".equals(type)) {
            reject(title, detail, open, getString(R.string.share_wrong_mime));
            return;
        }
        if (text == null) {
            reject(title, detail, open, getString(R.string.share_empty));
            return;
        }
        Creds.Profile profile = Creds.profile(this);
        if (profile == null || profile.instanceId.isEmpty()) {
            // Setup (which registers the durable periodic drain) has not happened yet.
            reject(title, detail, open, getString(R.string.share_not_setup));
            return;
        }
        String problem = OutboxPolicy.validateShare(text);
        if (problem != null) {
            int resId = getResources().getIdentifier(problem, "string", getPackageName());
            String msg = resId != 0 ? getString(resId, text.getBytes().length) : problem;
            reject(title, detail, open, msg);
            return;
        }

        String id;
        try {
            OutboxStore db = new OutboxStore(this);
            id = db.insert(profile.instanceId, profile.userId, profile.email, profile.endpoint,
                    text, profile.defaultCollectionId);
            WorkScheduler.drainNow(this);
        } catch (Exception e) {
            reject(title, detail, open, getString(R.string.share_storage_error));
            return;
        }

        // Durable now: row committed with stable id + identity + destination snapshot.
        title.setText(R.string.share_saved);
        detail.setText(getString(R.string.share_saved_detail, profile.email));
        if (profile.defaultCollectionName != null && !profile.defaultCollectionName.isEmpty()) {
            destination.setText(getString(R.string.share_default_destination, profile.defaultCollectionName));
        }
        open.setVisibility(View.VISIBLE);
        open.setOnClickListener(v -> {
            finish();
            startActivity(new Intent(this, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        });
        new Handler(Looper.getMainLooper()).postDelayed(this::finish, CONFIRM_VISIBLE_MS);
    }

    private void reject(TextView title, TextView detail, Button open, String message) {
        title.setText(R.string.share_rejected_title);
        detail.setText(message);
        open.setVisibility(View.VISIBLE);
        open.setText(R.string.share_open_app);
        open.setOnClickListener(v -> {
            finish();
            startActivity(new Intent(this, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        });
        // Rejected shares are not persisted; no auto-finish race — let the user read it.
    }
}
