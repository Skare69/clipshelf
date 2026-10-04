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
import dev.clipshelf.app.work.DurableShare;
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
    private static final String KEY_COMMITTED = "share_committed";
    private static final String KEY_DETAIL = "share_detail";
    private static final String KEY_DESTINATION = "share_destination";

    /** Set once the outbox row is committed; drives replay across recreation. */
    private boolean committed;
    private String savedDetail, savedDestination;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_share);

        Intent intent = getIntent();
        TextView title = findViewById(R.id.share_title);
        TextView detail = findViewById(R.id.share_detail);
        TextView destination = findViewById(R.id.share_destination);
        Button open = findViewById(R.id.share_open);

        // Recreation (rotation or process-death restore) replays the SAME
        // share intent: re-show the committed result instead of inserting a
        // duplicate outbox row. Uncommitted replays re-run the checks below.
        if (savedInstanceState != null && savedInstanceState.getBoolean(KEY_COMMITTED)) {
            showSaved(title, detail, destination, open,
                    savedInstanceState.getString(KEY_DETAIL),
                    savedInstanceState.getString(KEY_DESTINATION));
            return;
        }

        String type = intent == null ? null : intent.getType();
        Object rawText = intent != null && intent.getExtras() != null
                ? intent.getExtras().get(Intent.EXTRA_TEXT) : null;
        String text = OutboxPolicy.sharedText(rawText);

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

        OutboxStore db;
        long[] usage;
        try {
            db = new OutboxStore(this);
            usage = db.usage();
        } catch (Exception e) {
            // The store may be unusable (locked/corrupt): reject, never crash.
            reject(title, detail, open, getString(R.string.share_storage_error));
            return;
        }
        if (OutboxPolicy.outboxFull((int) usage[0], usage[1],
                text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length)) {
            reject(title, detail, open, getString(R.string.share_outbox_full,
                    usage[0], OutboxPolicy.MAX_OUTBOX_ROWS));
            return;
        }

        String detailText = getString(R.string.share_saved_detail, profile.email);
        String destinationText = profile.defaultCollectionName == null
                || profile.defaultCollectionName.isEmpty()
                ? "" : getString(R.string.share_default_destination, profile.defaultCollectionName);
        String[] committed = new String[1];
        boolean saved = DurableShare.commit(() -> committed[0] = db.insert(profile.instanceId,
                profile.userId, profile.email, profile.endpoint, text, profile.defaultCollectionId));
        if (!saved || committed[0] == null) {
            // The store could not commit the row: never claim "saved".
            reject(title, detail, open, getString(R.string.share_storage_error));
            return;
        }

        // Durable now: row committed with stable id + identity + destination snapshot.
        // Prompt scheduling is an accelerator only — the periodic drain
        // registered at setup delivers even when enqueue fails here, so a
        // scheduling failure must never read as "could not save".
        DurableShare.accelerate(() -> WorkScheduler.drainNow(this));
        showSaved(title, detail, destination, open, detailText, destinationText);
    }

    /** Shows the saved confirm and remembers it for replay across recreation. */
    private void showSaved(TextView title, TextView detail, TextView destination, Button open,
                           String detailText, String destinationText) {
        committed = true;
        savedDetail = detailText;
        savedDestination = destinationText;
        title.setText(R.string.share_saved);
        detail.setText(detailText);
        if (!destinationText.isEmpty()) {
            destination.setText(destinationText);
        }
        openApp(open);
        new Handler(Looper.getMainLooper()).postDelayed(this::finish, CONFIRM_VISIBLE_MS);
    }

    /** The one exit back into the app, shared by saved and rejected outcomes. */
    private void openApp(Button open) {
        open.setVisibility(View.VISIBLE);
        open.setOnClickListener(v -> {
            finish();
            startActivity(new Intent(this, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (committed) {
            outState.putBoolean(KEY_COMMITTED, true);
            outState.putString(KEY_DETAIL, savedDetail);
            outState.putString(KEY_DESTINATION, savedDestination);
        }
    }

    private void reject(TextView title, TextView detail, Button open, String message) {
        title.setText(R.string.share_rejected_title);
        detail.setText(message);
        open.setText(R.string.share_open_app);
        openApp(open);
        // Rejected shares are not persisted; no auto-finish race — let the user read it.
    }
}
