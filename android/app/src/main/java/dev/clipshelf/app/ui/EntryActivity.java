package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import android.app.AlertDialog;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/**
 * Entry detail: findings text (inert DOM textContent-equivalent via setText,
 * never HTML rendering), sources, authorized saved material, per-job states
 * with server retry, and collection-scoped removal rules from the API.
 */
public class EntryActivity extends Activity {

    private Creds.Session session;
    private String entryId;
    private Api.EntryDetail detail;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_entry);
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
        Async.go(() -> Api.entry(session.profile.endpoint, session.token, entryId),
                (detail, error) -> {
                    if (error != null) {
                        String m = Ui.message(this, error);
                        status.setText(m);
                        if (!(error instanceof Api.ApiException)) {
                            status.setVisibility(View.GONE);
                        }
                        return;
                    }
                    status.setVisibility(View.GONE);
                    this.detail = detail;
                    render();
                });
    }

    private void render() {
        Api.Entry e = detail.entry;
        TextView title = findViewById(R.id.entry_title);
        TextView body = findViewById(R.id.entry_body);
        LinearLayout containers = findViewById(R.id.entry_containers);
        Button remove = findViewById(R.id.entry_remove);
        Button open = findViewById(R.id.entry_open_url);
        containers.removeAllViews();

        title.setText(e.title.isEmpty() ? (e.url.isEmpty() ? e.id : e.url) : e.title);
        StringBuilder sb = new StringBuilder();
        sb.append("prompt".equals(e.kind) ? getString(R.string.entry_kind_prompt)
                : getString(R.string.entry_kind_link));
        if (!e.cat.isEmpty()) {
            sb.append("  ·  ").append(e.cat);
        }
        if (!e.contributors.isEmpty()) {
            sb.append("\n").append(getString(R.string.entry_contributors_label,
                    String.join(", ", e.contributors)));
        }
        if (!e.tags.isEmpty()) {
            sb.append("\n").append(getString(R.string.entry_tags_label, String.join(", ", e.tags)));
        }
        if (!e.desc.isEmpty()) {
            sb.append("\n\n").append(e.desc);
        }
        if (!e.text.isEmpty()) {
            sb.append("\n\n").append(e.text);
        }
        body.setText(sb.toString()); // inert text, never interpreted as markup

        open.setVisibility(e.url.isEmpty() ? View.GONE : View.VISIBLE);
        open.setOnClickListener(v -> startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(e.url))));
        remove.setVisibility(e.canRemove ? View.VISIBLE : View.GONE);
        remove.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setMessage(R.string.entry_remove_confirm)
                .setPositiveButton(R.string.ok, (d, w) -> deleteEntry())
                .setNegativeButton(R.string.cancel, null)
                .show());

        if (!detail.assets.isEmpty()) {
            addHeading(containers, getString(R.string.entry_assets_label));
            for (Api.Asset asset : detail.assets) {
                Button b = new Button(this, null, android.R.attr.borderlessButtonStyle);
                b.setText(getString(R.string.asset_view) + "  ·  " + (asset.name.isEmpty()
                        ? asset.id : asset.name) + "  (" + asset.kind + ")");
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
                tv.setText("• " + source);
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
            sb.append('\n').append(getString(R.string.job_screened_label, job.screeningWarnings));
        }
        if (job.error != null && !job.error.isEmpty()) {
            sb.append('\n').append(getString(R.string.job_error_label, job.error));
        }
        tv.setText(sb.toString());
        tv.setTextIsSelectable(true);
        container.addView(tv);
        Button retry = new Button(this, null, android.R.attr.borderlessButtonStyle);
        retry.setText(R.string.entry_retry_job);
        retry.setOnClickListener(v -> {
            retry.setEnabled(false);
            Async.go(() -> {
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
        tv.setTextAppearance(this, android.R.style.TextAppearance_Material_Small);
        tv.setPadding(0, dp(20), 0, dp(4));
        container.addView(tv);
    }

    private void deleteEntry() {
        Async.go(() -> {
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
