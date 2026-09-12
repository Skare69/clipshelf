package dev.clipshelf.app.ui;

import android.app.Activity;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import android.app.AlertDialog;

import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/** Shared-collection membership management (server enforces owner rules). */
public class CollectionDetailActivity extends Activity {

    private Creds.Session session;
    private String collectionId;
    private List<Api.Member> members;
    private Adapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_collection_detail);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        collectionId = getIntent().getStringExtra("collectionId");
        setTitle(getIntent().getStringExtra("name"));

        ListView list = findViewById(R.id.members_list);
        adapter = new Adapter();
        list.setAdapter(adapter);

        EditText add = findViewById(R.id.member_add_email);
        Button addBtn = findViewById(R.id.member_add_go);
        addBtn.setOnClickListener(v -> {
            String email = add.getText().toString().trim();
            if (email.isEmpty()) {
                return;
            }
            addBtn.setEnabled(false);
            Async.go(() -> {
                Api.addMember(session.profile.endpoint, session.token, collectionId, email);
                return null;
            }, (r, e) -> {
                addBtn.setEnabled(true);
                if (e != null) {
                    Toast.makeText(this, Ui.message(this, e), Toast.LENGTH_LONG).show();
                } else {
                    add.setText("");
                    reload();
                }
            });
        });
        reload();
    }

    private void reload() {
        TextView status = findViewById(R.id.members_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.loading);
        Async.go(() -> Api.members(session.profile.endpoint, session.token, collectionId),
                (result, error) -> {
                    status.setVisibility(View.GONE);
                    if (error != null) {
                        status.setText(Ui.message(this, error));
                        return;
                    }
                    members = result;
                    adapter.notifyDataSetChanged();
                });
    }

    private class Adapter extends BaseAdapter {
        @Override
        public int getCount() {
            return members == null ? 0 : members.size();
        }

        @Override
        public Api.Member getItem(int position) {
            return members.get(position);
        }

        @Override
        public long getItemId(int position) {
            return position;
        }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            View v = convertView;
            if (v == null) {
                v = LayoutInflater.from(CollectionDetailActivity.this)
                        .inflate(R.layout.item_member, parent, false);
            }
            Api.Member m = getItem(position);
            TextView email = v.findViewById(R.id.member_email);
            email.setText(m.email + (m.isOwner ? getString(R.string.member_owner_suffix) : ""));
            Button remove = v.findViewById(R.id.member_remove);
            // The server rejects removing the owner or non-members; hide what cannot work.
            remove.setVisibility(m.isOwner ? View.GONE : View.VISIBLE);
            remove.setOnClickListener(iv -> new AlertDialog.Builder(CollectionDetailActivity.this)
                    .setMessage(getString(R.string.member_remove) + " " + m.email + "?")
                    .setPositiveButton(R.string.ok, (d, w) -> Async.go(() -> {
                        Api.removeMember(session.profile.endpoint, session.token, collectionId, m.id);
                        return null;
                    }, (r, e) -> {
                        if (e != null) {
                            Toast.makeText(CollectionDetailActivity.this,
                                    Ui.message(CollectionDetailActivity.this, e), Toast.LENGTH_LONG).show();
                        } else {
                            reload();
                        }
                    }))
                    .setNegativeButton(R.string.cancel, null)
                    .show());
            return v;
        }
    }
}
