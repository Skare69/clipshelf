package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/**
 * Online library browsing: collection navigation, search, sort, kind filter,
 * detail. Requires the server — there is deliberately no offline library
 * replica on the phone.
 */
public class LibraryActivity extends Activity {

    private static final int PAGE = 30;

    private Creds.Session session;
    private List<Api.Collection> collections = new ArrayList<>();
    private final List<Api.Entry> entries = new ArrayList<>();
    private Adapter adapter;
    private Spinner collectionSpinner;
    private boolean spinnerHooked;
    private String q = "";
    private String sort = "new";
    private String kind = "";
    private int offset;
    private int count;
    private String collectionId;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_library);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        collectionSpinner = findViewById(R.id.library_collection);
        EditText search = findViewById(R.id.library_search);
        Button searchBtn = findViewById(R.id.library_search_go);
        Button sortBtn = findViewById(R.id.library_sort);
        Button kindBtn = findViewById(R.id.library_kind);
        Button refresh = findViewById(R.id.library_refresh);
        ListView list = findViewById(R.id.library_list);
        adapter = new Adapter();
        list.setAdapter(adapter);
        list.setOnItemClickListener((p, v, pos, id) -> {
            int index = pos;
            if (index >= 0 && index < entries.size()) {
                Intent i = new Intent(this, EntryActivity.class);
                i.putExtra("entryId", entries.get(index).id);
                startActivity(i);
            }
        });
        searchBtn.setOnClickListener(v -> {
            q = search.getText().toString().trim();
            reload();
        });
        sortBtn.setOnClickListener(v -> {
            sort = "new".equals(sort) ? "az" : "new";
            sortBtn.setText("new".equals(sort) ? R.string.sort_new : R.string.sort_az);
            reload();
        });
        kindBtn.setOnClickListener(v -> {
            kind = kind.isEmpty() ? "link" : "link".equals(kind) ? "prompt" : "";
            kindBtn.setText(kind.isEmpty() ? R.string.kind_all
                    : "link".equals(kind) ? R.string.kind_links : R.string.kind_prompts);
            reload();
        });
        refresh.setOnClickListener(v -> reload());
        Button more = findViewById(R.id.library_more);
        more.setOnClickListener(v -> loadMore());
        reload();
    }

    private void reload() {
        offset = 0;
        entries.clear();
        adapter.notifyDataSetChanged();
        Async.go(() -> Api.me(session.profile.endpoint, session.token), (me, error) -> {
            if (error != null) {
                fail(error);
                return;
            }
            collections = me.collections;
            List<String> names = new ArrayList<>();
            for (Api.Collection c : collections) {
                names.add(c.name + (me.defaultCollectionId != null && me.defaultCollectionId.equals(c.id)
                        ? "  ★" : ""));
            }
            collectionSpinner.setAdapter(new ArrayAdapter<>(this,
                    android.R.layout.simple_spinner_dropdown_item, names));
            if (!spinnerHooked) {
                spinnerHooked = true;
                collectionSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
                    @Override
                    public void onItemSelected(AdapterView<?> p, View v, int pos, long id) {
                        String selected = pos >= 0 && pos < collections.size()
                                ? collections.get(pos).id : null;
                        // The initial layout also fires this callback; load only on real changes.
                        if (selected != null && !selected.equals(collectionId)) {
                            collectionId = selected;
                            reload();
                        }
                    }

                    @Override
                    public void onNothingSelected(AdapterView<?> p) {
                    }
                });
                int def = 0;
                for (int i = 0; i < collections.size(); i++) {
                    if (collections.get(i).id.equals(me.defaultCollectionId)) {
                        def = i;
                        break;
                    }
                }
                collectionSpinner.setSelection(def);
                collectionId = collections.isEmpty() ? null : collections.get(def).id;
            }
            if (collectionId != null) {
                loadMore();
            }
        });
    }

    private void loadMore() {
        if (collectionId == null) {
            return;
        }
        setStatus(getString(R.string.loading));
        Async.go(() -> Api.entries(session.profile.endpoint, session.token, collectionId,
                        q, sort, kind, offset, PAGE),
                (page, error) -> {
                    if (error != null) {
                        fail(error);
                        return;
                    }
                    setStatus("");
                    entries.addAll(page.entries);
                    count = page.count;
                    adapter.notifyDataSetChanged();
                    Button more = findViewById(R.id.library_more);
                    more.setVisibility(entries.size() < count ? View.VISIBLE : View.GONE);
                    TextView empty = findViewById(R.id.library_empty);
                    empty.setVisibility(entries.isEmpty() ? View.VISIBLE : View.GONE);
                });
        offset += PAGE;
    }

    private void fail(Exception error) {
        String message = Ui.message(this, error);
        if (!(error instanceof Api.ApiException)
                || (((Api.ApiException) error).code != 401 && ((Api.ApiException) error).code != 403)) {
            setStatus(message);
        }
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private void setStatus(String text) {
        TextView status = findViewById(R.id.library_status);
        status.setText(text);
        status.setVisibility(text.isEmpty() ? View.GONE : View.VISIBLE);
    }

    private class Adapter extends BaseAdapter {
        @Override
        public int getCount() {
            return entries.size();
        }

        @Override
        public Api.Entry getItem(int position) {
            return entries.get(position);
        }

        @Override
        public long getItemId(int position) {
            return position;
        }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            View v = convertView;
            if (v == null) {
                v = LayoutInflater.from(LibraryActivity.this)
                        .inflate(R.layout.item_entry, parent, false);
            }
            Api.Entry e = getItem(position);
            TextView title = v.findViewById(R.id.entry_title);
            TextView sub = v.findViewById(R.id.entry_sub);
            title.setText(e.title.isEmpty() ? (e.url.isEmpty() ? e.text : e.url) : e.title);
            title.setTextAlignment(View.TEXT_ALIGNMENT_VIEW_START);
            String kindLabel = "prompt".equals(e.kind)
                    ? getString(R.string.entry_kind_prompt) : getString(R.string.entry_kind_link);
            sub.setText(kindLabel + "  ·  " + (e.url.isEmpty() ? e.cat : e.url));
            return v;
        }
    }
}
