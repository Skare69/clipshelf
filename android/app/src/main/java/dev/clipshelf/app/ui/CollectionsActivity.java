package dev.clipshelf.app.ui;

import androidx.appcompat.app.AppCompatActivity;
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

import androidx.appcompat.app.AppCompatActivity;

import com.google.android.material.dialog.MaterialAlertDialogBuilder;

import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/** Shared-collection browsing, creation, and default-destination selection. */
public class CollectionsActivity extends AppCompatActivity {

    private Creds.Session session;
    private List<Api.Collection> collections;
    private String defaultCollectionId;
    private Adapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_collections);
        Ui.applyInsets(this);
        Ui.bottomNav(this);
        androidx.appcompat.widget.Toolbar toolbar = findViewById(R.id.toolbar);
        toolbar.setTitle(R.string.collections_title);
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
        Async.goUi(this, () -> Api.me(session.profile.endpoint, session.token), (me, error) -> {
            if (error != null) {
                status.setText(Ui.message(this, error));
                return;
            }
            status.setVisibility(View.GONE);
            collections = me.collections;
            defaultCollectionId = me.defaultCollectionId;
            adapter.notifyDataSetChanged();
        });
    }

    private void promptCreate() {
        EditText input = new EditText(this);
        input.setHint(R.string.collection_new_hint);
        new MaterialAlertDialogBuilder(this)
                .setTitle(R.string.collection_new)
                .setView(input)
                .setPositiveButton(R.string.collection_create, (d, w) -> {
                    String name = input.getText().toString().trim();
                    if (name.isEmpty()) {
                        return;
                    }
                    Async.goUi(this, () -> Api.createCollection(session.profile.endpoint, session.token, name),
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
            TextView defTag = v.findViewById(R.id.collection_default_tag);
            Button def = v.findViewById(R.id.collection_set_default);
            Button members = v.findViewById(R.id.collection_manage);

            boolean isDefault = c.id.equals(defaultCollectionId);
            name.setText(c.name);
            kind.setText("personal".equals(c.kind)
                    ? R.string.collection_kind_personal : R.string.collection_kind_shared);
            defTag.setVisibility(isDefault ? View.VISIBLE : View.GONE);

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
            // A card with no action shows no perforation and no empty action row.
            int actions = isDefault && !manageable ? View.GONE : View.VISIBLE;
            v.findViewById(R.id.collection_perf).setVisibility(actions);
            v.findViewById(R.id.collection_actions).setVisibility(actions);
            return v;
        }
    }
}
