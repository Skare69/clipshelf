package dev.clipshelf.app.ui;

import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import com.google.android.material.dialog.MaterialAlertDialogBuilder;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/**
 * Entry label detail: routing block for the category, title, From link,
 * Contents, prompt and install in monospace blocks with copy, tags, sources,
 * authorized saved material, per-job states with server retry, and
 * collection-scoped removal rules from the API. All text lands through
 * setText, never interpreted as markup.
 */
public class EntryActivity extends AppCompatActivity {

    private Creds.Session session;
    private String entryId;
    private Api.EntryDetail detail;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_entry);
        Ui.applyInsets(this);
        Ui.up(this);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        entryId = getIntent().getStringExtra("entryId");
        load();
    }

    private void load() {
        TextView status = findViewById(R.id.entry_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.loading);
        Async.goUi(this, () -> Api.entry(session.profile.endpoint, session.token, entryId),
                (detail, error) -> {
                    if (error != null) {
                        String m = Ui.message(this, error);
                        status.setText(m);
                        return;
                    }
                    status.setVisibility(View.GONE);
                    this.detail = detail;
                    render();
                });
    }

    private void render() {
        Api.Entry e = detail.entry;
        TextView routing = findViewById(R.id.entry_routing);
        TextView title = findViewById(R.id.entry_title);
        TextView from = findViewById(R.id.entry_from);
        TextView body = findViewById(R.id.entry_body);
        TextView promptLabel = findViewById(R.id.entry_prompt_label);
        TextView promptText = findViewById(R.id.entry_prompt_text);
        Button copyPrompt = findViewById(R.id.entry_copy_prompt);
        TextView installLabel = findViewById(R.id.entry_install_label);
        TextView installText = findViewById(R.id.entry_install_text);
        Button copyInstall = findViewById(R.id.entry_copy_install);
        ViewGroup tags = findViewById(R.id.entry_tags);
        LinearLayout containers = findViewById(R.id.entry_containers);
        Button open = findViewById(R.id.entry_open_url);
        Button remove = findViewById(R.id.entry_remove);

        boolean prompt = "prompt".equals(e.kind);
        if (!e.cats.isEmpty()) {
            routing.setText(e.catLabel());
            routing.setVisibility(View.VISIBLE);
        }
        title.setText(e.title.isEmpty() ? (e.url.isEmpty() ? e.text : e.url) : e.title);
        if (e.url.isEmpty()) {
            from.setVisibility(View.GONE);
        } else {
            from.setText(getString(R.string.from_format, e.url));
            from.setVisibility(View.VISIBLE);
            from.setOnClickListener(v ->
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(e.url))));
        }

        StringBuilder sb = new StringBuilder(!e.summary.isEmpty() ? e.summary : e.desc);
        if (!e.contributors.isEmpty()) {
            sb.append(sb.length() == 0 ? "" : "\n\n")
                    .append(getString(R.string.entry_contributors_label,
                            String.join(", ", e.contributors)));
        }
        // Link entries keep their fetched page text here; prompt entries show
        // their text in the prompt block below.
        if (!prompt && !e.text.isEmpty()) {
            sb.append(sb.length() == 0 ? "" : "\n\n").append(e.text);
        }
        body.setText(sb.toString()); // inert text, never interpreted as markup

        boolean hasPrompt = prompt && !e.text.isEmpty();
        promptLabel.setVisibility(hasPrompt ? View.VISIBLE : View.GONE);
        promptText.setVisibility(hasPrompt ? View.VISIBLE : View.GONE);
        copyPrompt.setVisibility(hasPrompt ? View.VISIBLE : View.GONE);
        if (hasPrompt) {
            promptText.setText(e.text);
            copyPrompt.setOnClickListener(v ->
                    Ui.copy(this, findViewById(R.id.entry_copy_prompt), "prompt", e.text));
        }
        boolean hasInstall = !e.install.isEmpty();
        installLabel.setVisibility(hasInstall ? View.VISIBLE : View.GONE);
        installText.setVisibility(hasInstall ? View.VISIBLE : View.GONE);
        copyInstall.setVisibility(hasInstall ? View.VISIBLE : View.GONE);
        if (hasInstall) {
            installText.setText(e.install);
            copyInstall.setOnClickListener(v ->
                    Ui.copy(this, findViewById(R.id.entry_copy_install), "install", e.install));
        }

        tags.removeAllViews();
        for (String tag : e.tags) {
            TextView tv = Ui.tag(this, tag);
            tv.setMaxLines(1);
            tags.addView(tv);
        }
        tags.setVisibility(tags.getChildCount() == 0 ? View.GONE : View.VISIBLE);

        containers.removeAllViews();
        open.setVisibility(e.url.isEmpty() ? View.GONE : View.VISIBLE);
        open.setOnClickListener(v -> startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(e.url))));
        remove.setVisibility(e.canRemove ? View.VISIBLE : View.GONE);
        remove.setOnClickListener(v -> new MaterialAlertDialogBuilder(this)
                .setMessage(R.string.entry_remove_confirm)
                .setPositiveButton(R.string.ok, (d, w) -> deleteEntry())
                .setNegativeButton(R.string.cancel, null)
                .show());

        if (!detail.assets.isEmpty()) {
            addHeading(containers, getString(R.string.entry_assets_label));
            for (Api.Asset asset : detail.assets) {
                Button b = new Button(new android.view.ContextThemeWrapper(this,
                        R.style.Widget_Parcel_Button_Text));
                b.setText(getString(R.string.asset_view_named,
                        asset.name.isEmpty() ? asset.id : asset.name, asset.kind));
                b.setOnClickListener(v -> {
                    Intent i = new Intent(this, AssetActivity.class);
                    i.putExtra("url", asset.url);
                    i.putExtra("name", asset.name);
                    i.putExtra("contentType", asset.contentType);
                    i.putExtra("kind", asset.kind);
                    startActivity(i);
                });
                containers.addView(b);
            }
        }

        if (!e.sources.isEmpty()) {
            addHeading(containers, getString(R.string.entry_sources_label));
            for (String source : e.sources) {
                TextView tv = new TextView(this);
                tv.setText(getString(R.string.entry_source_item, source));
                tv.setTextAppearance(this, R.style.TextAppearance_Parcel_T3);
                tv.setTextIsSelectable(true);
                containers.addView(tv);
            }
        }

        if (!detail.jobs.isEmpty()) {
            addHeading(containers, getString(R.string.entry_jobs_label));
            for (Api.Job job : detail.jobs) {
                addJob(containers, job);
            }
        }
    }

    private void addJob(LinearLayout container, Api.Job job) {
        TextView tv = new TextView(this);
        StringBuilder sb = new StringBuilder();
        sb.append(job.url).append('\n')
                .append(getString(R.string.job_state_label, job.state)).append("  ·  ")
                .append(getString(R.string.job_acquisition_label, job.acquisition)).append("  ·  ")
                .append(getString(R.string.job_interpretation_label, job.interpretation));
        if (job.guardrail) {
            sb.append('\n').append(getString(R.string.job_guardrail_label));
        }
        if (job.screeningWarnings > 0) {
            sb.append('\n').append(getResources().getQuantityString(
                    R.plurals.job_screened_label, job.screeningWarnings, job.screeningWarnings));
        }
        if (job.error != null && !job.error.isEmpty()) {
            sb.append('\n').append(getString(R.string.job_error_label, job.error));
        }
        tv.setText(sb.toString());
        tv.setTextAppearance(this, R.style.TextAppearance_Parcel_T3);
        tv.setTextIsSelectable(true);
        container.addView(tv);
        Button retry = new Button(new android.view.ContextThemeWrapper(this,
                R.style.Widget_Parcel_Button_Text));
        retry.setText(R.string.entry_retry_job);
        retry.setOnClickListener(v -> {
            retry.setEnabled(false);
            Async.goUi(this, () -> {
                Api.retryJob(session.profile.endpoint, session.token, job.id);
                return null;
            }, (r, e) -> {
                retry.setEnabled(true);
                if (e != null) {
                    Toast.makeText(this, Ui.message(this, e), Toast.LENGTH_LONG).show();
                } else {
                    Toast.makeText(this, R.string.outbox_toast_requeued, Toast.LENGTH_SHORT).show();
                    load();
                }
            });
        });
        container.addView(retry);
    }

    private void addHeading(LinearLayout container, String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextAppearance(this, R.style.TextAppearance_Parcel_T1);
        tv.setTextColor(getColor(R.color.ink2));
        tv.setPadding(0, dp(20), 0, dp(4));
        container.addView(tv);
    }

    private void deleteEntry() {
        Async.goUi(this, () -> {
            Api.deleteEntry(session.profile.endpoint, session.token, entryId);
            return null;
        }, (r, e) -> {
            if (e != null) {
                Toast.makeText(this, Ui.message(this, e), Toast.LENGTH_LONG).show();
                return;
            }
            Toast.makeText(this, R.string.entry_removed, Toast.LENGTH_SHORT).show();
            finish();
        });
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
