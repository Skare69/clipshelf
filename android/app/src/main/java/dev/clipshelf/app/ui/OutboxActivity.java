package dev.clipshelf.app.ui;

import android.app.Activity;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import android.app.AlertDialog;

import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.outbox.OutboxStore;
import dev.clipshelf.app.work.WorkScheduler;

/**
 * Offline-readable outbox for the active profile, grouped implicitly by row
 * identity; rows saved under another account/server are labeled and skipped
 * by delivery, never rerouted. Shows the platform background limits honestly.
 */
public class OutboxActivity extends Activity {

    private OutboxStore db;
    private List<OutboxStore.Row> rows;
    private Adapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_outbox);
        db = new OutboxStore(this);
        ListView list = findViewById(R.id.outbox_list);

        View header = LayoutInflater.from(this).inflate(R.layout.item_outbox_header, list, false);
        Button retry = header.findViewById(R.id.outbox_retry);
        Button clear = header.findViewById(R.id.outbox_clear);
        retry.setOnClickListener(v -> {
            WorkScheduler.drainNow(this);
            Toast.makeText(this, R.string.outbox_toast_requeued, Toast.LENGTH_SHORT).show();
        });
        clear.setOnClickListener(v -> {
            db.deleteDelivered();
            reload();
            Toast.makeText(this, R.string.outbox_toast_cleared, Toast.LENGTH_SHORT).show();
        });
        list.addHeaderView(header);

        View footer = LayoutInflater.from(this).inflate(R.layout.item_outbox_limits, list, false);
        list.addFooterView(footer);

        adapter = new Adapter();
        list.setAdapter(adapter);
        list.setOnItemClickListener((parent, view, position, id) -> {
            int index = position - list.getHeaderViewsCount();
            if (index >= 0 && index < rows.size()) {
                actOn(rows.get(index));
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        reload();
    }

    private void reload() {
        rows = db.listAll();
        adapter.notifyDataSetChanged();
        TextView empty = findViewById(R.id.outbox_empty);
        empty.setVisibility(rows.isEmpty() ? View.VISIBLE : View.GONE);
    }

    private void actOn(OutboxStore.Row row) {
        if (row.state != OutboxStore.STATE_PAUSED && row.state != OutboxStore.STATE_REJECTED) {
            return;
        }
        new AlertDialog.Builder(this)
                .setMessage(row.lastError != null ? row.lastError : getString(R.string.outbox_retry_now))
                .setPositiveButton(R.string.outbox_requeue, (d, w) -> {
                    db.requeue(row.id);
                    WorkScheduler.drainNow(this);
                    reload();
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private class Adapter extends BaseAdapter {
        @Override
        public int getCount() {
            return rows == null ? 0 : rows.size();
        }

        @Override
        public OutboxStore.Row getItem(int position) {
            return rows.get(position);
        }

        @Override
        public long getItemId(int position) {
            return position;
        }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            View v = convertView;
            if (v == null) {
                v = LayoutInflater.from(OutboxActivity.this).inflate(R.layout.item_outbox, parent, false);
            }
            OutboxStore.Row row = getItem(position);
            TextView state = v.findViewById(R.id.outbox_state);
            TextView text = v.findViewById(R.id.outbox_text);
            TextView meta = v.findViewById(R.id.outbox_meta);
            TextView error = v.findViewById(R.id.outbox_error);

            text.setText(row.text.length() > 200 ? row.text.substring(0, 200) + "…" : row.text);

            Creds.Profile profile = Creds.profile(OutboxActivity.this);
            boolean otherIdentity = profile == null
                    || !row.instanceId.equals(profile.instanceId) || !row.userId.equals(profile.userId);

            int stateRes;
            int stateColor;
            switch (row.state) {
                case OutboxStore.STATE_DELIVERED:
                    stateRes = R.string.outbox_state_delivered;
                    stateColor = R.color.status_ok;
                    break;
                case OutboxStore.STATE_REJECTED:
                    stateRes = R.string.outbox_state_rejected;
                    stateColor = R.color.status_error;
                    break;
                case OutboxStore.STATE_PAUSED:
                    stateRes = R.string.outbox_state_paused;
                    stateColor = R.color.status_paused;
                    break;
                default:
                    stateRes = R.string.outbox_state_queued;
                    stateColor = R.color.text_secondary;
            }
            state.setText(getString(stateRes) + (row.attempts > 0 ? "  ·  " + row.attempts : ""));
            state.setTextColor(getColor(stateColor));

            if (otherIdentity) {
                meta.setText(getString(R.string.outbox_other_account, row.accountEmail, row.endpoint));
            } else {
                String dest = row.requestedCollectionId != null
                        ? getString(R.string.share_default_destination, row.requestedCollectionId) + "  ·  " : "";
                meta.setText(dest + row.endpoint);
            }
            meta.setTextColor(getColor(otherIdentity ? R.color.status_paused : R.color.text_secondary));

            String err = row.lastError != null ? row.lastError : "";
            error.setText(err);
            error.setVisibility(err.isEmpty() ? View.GONE : View.VISIBLE);
            return v;
        }
    }
}
