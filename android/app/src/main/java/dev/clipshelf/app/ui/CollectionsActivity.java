package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
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

/** Shared-collection browsing, creation, and default-destination selection. */
public class CollectionsActivity extends Activity {

    private Creds.Session session;
    private List<Api.Collection> collections;
    private String defaultCollectionId;
    private Adapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_collections);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        ListView list = findViewById(R.id.collections_list);
        adapter = new Adapter();
        list.setAdapter(adapter);
        list.setOnItemClickListener((p, v, pos, id) -> {
            Api.Collection c = collections.get(pos);
            if (c.canManage && !"personal".equals(c.kind)) {
                Intent i = new Intent(this, CollectionDetailActivity.class);
                i.putExtra("collectionId", c.id);
                i.putExtra("name", c.name);
                startActivity(i);
            }
        });
        Button create = findViewById(R.id.collections_create);
        create.setOnClickListener(v -> promptCreate());
        reload();
    }

    private void reload() {
        TextView status = findViewById(R.id.collections_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.loading);
        Async.go(() -> Api.me(session.profile.endpoint, session.token), (me, error) -> {
            status.setVisibility(View.GONE);
            if (error != null) {
                status.setText(Ui.message(this, error));
                return;
            }
            collections = me.collections;
            defaultCollectionId = me.defaultCollectionId;
            adapter.notifyDataSetChanged();
        });
    }

    private void promptCreate() {
        EditText input = new EditText(this);
        input.setHint(R.string.collection_new_hint);
        new AlertDialog.Builder(this)
                .setTitle(R.string.collection_new)
                .setView(input)
                .setPositiveButton(R.string.collection_create, (d, w) -> {
                    String name = input.getText().toString().trim();
                    if (name.isEmpty()) {
                        return;
                    }
                    Async.go(() -> Api.createCollection(session.profile.endpoint, session.token, name),
                            (c, error) -> {
                                if (error != null) {
                                    Toast.makeText(this, Ui.message(this, error), Toast.LENGTH_LONG).show();
                                } else {
                                    reload();
                                }
                            });
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void setDefault(Api.Collection c) {
        Async.go(() -> Api.setDefaultCollection(session.profile.endpoint, session.token, c.id),
                (me, error) -> {
                    if (error != null) {
                        Toast.makeText(this, Ui.message(this, error), Toast.LENGTH_LONG).show();
                        return;
                    }
                    String name = "";
                    for (Api.Collection col : me.collections) {
                        if (col.id.equals(me.defaultCollectionId)) {
                            name = col.name;
                            break;
                        }
                    }
                    Creds.updateDefaultCollection(this, me.defaultCollectionId, name);
                    defaultCollectionId = me.defaultCollectionId;
                    adapter.notifyDataSetChanged();
                    Toast.makeText(this, R.string.collection_default_set, Toast.LENGTH_SHORT).show();
                });
    }

    private class Adapter extends BaseAdapter {
        @Override
        public int getCount() {
            return collections == null ? 0 : collections.size();
        }

        @Override
        public Api.Collection getItem(int position) {
            return collections.get(position);
        }

        @Override
        public long getItemId(int position) {
            return position;
        }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            View v = convertView;
            if (v == null) {
                v = LayoutInflater.from(CollectionsActivity.this)
                        .inflate(R.layout.item_collection, parent, false);
            }
            Api.Collection c = getItem(position);
            TextView name = v.findViewById(R.id.collection_name);
            TextView kind = v.findViewById(R.id.collection_kind);
            Button def = v.findViewById(R.id.collection_set_default);
            Button members = v.findViewById(R.id.collection_manage);

            boolean isDefault = c.id.equals(defaultCollectionId);
            name.setText(c.name + (isDefault ? "  ★" : ""));
            kind.setText("personal".equals(c.kind)
                    ? R.string.collection_kind_personal : R.string.collection_kind_shared);

            def.setVisibility(isDefault ? View.GONE : View.VISIBLE);
            def.setOnClickListener(iv -> setDefault(c));
            boolean manageable = c.canManage && !"personal".equals(c.kind);
            members.setVisibility(manageable ? View.VISIBLE : View.GONE);
            members.setOnClickListener(iv -> {
                Intent i = new Intent(CollectionsActivity.this, CollectionDetailActivity.class);
                i.putExtra("collectionId", c.id);
                i.putExtra("name", c.name);
                startActivity(i);
            });
            return v;
        }
    }
}
