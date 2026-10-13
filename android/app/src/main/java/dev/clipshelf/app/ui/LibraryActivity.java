package dev.clipshelf.app.ui;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.widget.Toolbar;

import com.google.android.material.chip.Chip;
import com.google.android.material.chip.ChipGroup;
import com.google.android.material.textfield.TextInputEditText;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/**
 * Online library browsing: collection navigation, search, sort, kind filter,
 * category chips, entries as labels. Requires the server — there is
 * deliberately no offline library replica on the phone.
 */
public class LibraryActivity extends AppCompatActivity {

    private static final int PAGE = 30;
    private static final int MAX_TAGS = 4;

    private Creds.Session session;
    private List<Api.Collection> collections = new ArrayList<>();
    private String defaultCollectionId;
    private final List<Api.Entry> entries = new ArrayList<>();
    private final List<Api.Entry> visible = new ArrayList<>();
    private Adapter adapter;
    private String q = "";
    private String sort = "new";
    private String kind = "";
    /** Selected category chip; "" is All. */
    private String cat = "";
    private final PageTracker pages = new PageTracker();
    private int count;
    private String collectionId;
    private MenuItem sortItem;
    private MenuItem kindItem;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_library);
        Ui.applyInsets(this);
        Ui.bottomNav(this);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        Toolbar toolbar = findViewById(R.id.toolbar);
        toolbar.setTitle(R.string.library_title);
        // Tapping the title/subtitle area switches the collection.
        toolbar.setOnClickListener(v -> showCollectionMenu(toolbar));
        androidx.core.view.ViewCompat.replaceAccessibilityAction(toolbar,
                androidx.core.view.accessibility.AccessibilityNodeInfoCompat.AccessibilityActionCompat.ACTION_CLICK,
                getString(R.string.choose_collection), null);
        toolbar.inflateMenu(R.menu.menu_library);
        TextInputEditText search = findViewById(R.id.library_search);
        ListView list = findViewById(R.id.library_list);
        adapter = new Adapter();
        list.setAdapter(adapter);
        list.setOnItemClickListener((p, v, pos, id) -> {
            if (pos >= 0 && pos < visible.size()) {
                Intent i = new Intent(this, EntryActivity.class);
                i.putExtra("entryId", visible.get(pos).id);
                startActivity(i);
            }
        });
        search.setOnEditorActionListener((v, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_SEARCH) {
                q = v.getText().toString().trim();
                reload();
                return true;
            }
            return false;
        });
        toolbar.inflateMenu(R.menu.menu_library);
        toolbar.getMenu().getItem(0).setShowAsAction(android.view.MenuItem.SHOW_AS_ACTION_IF_ROOM);
        toolbar.setOnMenuItemClickListener(this::onMenuItem);
        sortItem = toolbar.getMenu().findItem(R.id.menu_sort);
        kindItem = toolbar.getMenu().findItem(R.id.menu_kind);
        updateMenuTitles();
        Button more = findViewById(R.id.library_more);
        more.setOnClickListener(v -> loadMore());
        reload();
    }

    private boolean onMenuItem(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.menu_sort) {
            sort = "new".equals(sort) ? "az" : "new";
            updateMenuTitles();
            reload();
            return true;
        }
        if (id == R.id.menu_kind) {
            kind = kind.isEmpty() ? "link" : "link".equals(kind) ? "prompt" : "";
            updateMenuTitles();
            reload();
            return true;
        }
        if (id == R.id.menu_refresh) {
            reload();
            return true;
        }
        return false;
    }

    private void updateMenuTitles() {
        sortItem.setTitle("new".equals(sort) ? R.string.sort_new : R.string.sort_az);
        kindItem.setTitle(kind.isEmpty() ? R.string.kind_all
                : "link".equals(kind) ? R.string.kind_links : R.string.kind_prompts);
    }

    private void reload() {
        // Re-read the session: Settings can adopt a new endpoint+token and
        // revoke the old one while this activity instance stays alive
        // (CLEAR_TOP|SINGLE_TOP returns here without onCreate).
        Creds.Session now = Creds.session(this);
        if (now == null) {
            Ui.sessionExpired(this);
            return;
        }
        session = now;
        final int gen = pages.reload();
        entries.clear();
        applyFilter();
        Async.goUi(this, () -> Api.me(session.profile.endpoint, session.token), (me, error) -> {
            if (!pages.current(gen)) return;
            if (error != null) {
                fail(error);
                return;
            }
            collections = me.collections;
            defaultCollectionId = me.defaultCollectionId;
            // First load opens the default collection; later loads keep the choice.
            if (collectionId == null) {
                collectionId = defaultCollectionId != null && hasCollection(defaultCollectionId)
                        ? defaultCollectionId
                        : (collections.isEmpty() ? null : collections.get(0).id);
            }
            updateCollectionSubtitle();
            if (collectionId != null) {
                loadMore();
            }
        });
    }

    private boolean hasCollection(String id) {
        for (Api.Collection c : collections) {
            if (c.id.equals(id)) {
                return true;
            }
        }
        return false;
    }

    private Api.Collection currentCollection() {
        for (Api.Collection c : collections) {
            if (c.id.equals(collectionId)) {
                return c;
            }
        }
        return null;
    }

    /** "Personal (default)" — the word, never a glyph. */
    private String collectionLabel(Api.Collection c) {
        boolean def = c.id.equals(defaultCollectionId);
        return def ? c.name + " (" + getString(R.string.default_collection_marker) + ")" : c.name;
    }

    private void updateCollectionSubtitle() {
        Toolbar toolbar = findViewById(R.id.toolbar);
        Api.Collection current = currentCollection();
        if (current == null) {
            toolbar.setSubtitle(null);
            return;
        }
        // A drawn drop-down arrow marks the subtitle as the collection menu.
        android.text.SpannableString label = new android.text.SpannableString(collectionLabel(current) + "  ");
        android.graphics.drawable.Drawable arrow = androidx.appcompat.content.res.AppCompatResources
                .getDrawable(this, R.drawable.ic_arrow_drop_down);
        if (arrow != null) {
            int size = Math.round(18 * getResources().getDisplayMetrics().density);
            arrow = arrow.mutate();
            arrow.setBounds(0, 0, size, size);
            arrow.setTint(getColor(R.color.ink2));
            label.setSpan(new android.text.style.ImageSpan(arrow, android.text.style.DynamicDrawableSpan.ALIGN_BOTTOM),
                    label.length() - 1, label.length(), android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        toolbar.setSubtitle(label);
    }

    /** Popup of the accessible collections; picking one switches the library. */
    private void showCollectionMenu(View anchor) {
        if (collections.isEmpty()) {
            return;
        }
        android.widget.PopupMenu popup = new android.widget.PopupMenu(this, anchor);
        for (int i = 0; i < collections.size(); i++) {
            popup.getMenu().add(android.view.Menu.NONE, i, i, collectionLabel(collections.get(i)));
        }
        popup.setOnMenuItemClickListener(item -> {
            Api.Collection picked = collections.get(item.getItemId());
            if (picked.id.equals(collectionId)) {
                return true;
            }
            collectionId = picked.id;
            updateCollectionSubtitle();
            reload();
            return true;
        });
        popup.show();
    }

    private void loadMore() {
        if (collectionId == null) {
            return;
        }
        final int gen = pages.generation();
        final int from = pages.offset();
        final String fCollection = collectionId;
        final String fq = q;
        final String fSort = sort;
        final String fKind = kind;
        setStatus(getString(R.string.loading));
        Async.goUi(this, () -> Api.entries(session.profile.endpoint, session.token, fCollection,
                        fq, fSort, fKind, from, PAGE),
                (page, error) -> {
                    if (!pages.current(gen)) return;
                    if (error != null) {
                        fail(error);
                        return;
                    }
                    if (!pages.commit(gen, from, page.entries.size())) return;
                    setStatus("");
                    entries.addAll(page.entries);
                    count = page.count;
                    rebuildChips();
                    applyFilter();
                    Button more = findViewById(R.id.library_more);
                    more.setVisibility(entries.size() < count ? View.VISIBLE : View.GONE);
                    TextView empty = findViewById(R.id.library_empty);
                    empty.setVisibility(visible.isEmpty() ? View.VISIBLE : View.GONE);
                });
    }

    /** Chip per category observed in the loaded entries; counts stay honest. */
    private void rebuildChips() {
        ChipGroup cats = findViewById(R.id.library_cats);
        cats.setSingleLine(true);
        cats.setSelectionRequired(true);
        cats.setSingleSelection(true);
        cats.setChipSpacingHorizontal((int) (8 * getResources().getDisplayMetrics().density));
        Map<String, Integer> counts = new LinkedHashMap<>();
        for (Api.Entry e : entries) {
            String label = e.catLabel();
            counts.put(label, counts.getOrDefault(label, 0) + 1);
        }
        // A category picked before a reload may no longer exist: fall back to
        // All, or the stale filter empties the list behind an unchecked chip.
        if (!cat.isEmpty() && !counts.containsKey(cat)) {
            cat = "";
        }
        cats.removeAllViews();
        addChip(cats, "", counts.values().stream().mapToInt(Integer::intValue).sum());
        for (Map.Entry<String, Integer> c : counts.entrySet()) {
            addChip(cats, c.getKey(), c.getValue());
        }
    }

    private void addChip(ChipGroup group, String label, int n) {
        Chip chip = (Chip) getLayoutInflater().inflate(R.layout.item_chip, group, false);
        chip.setText("".equals(label) ? getString(R.string.kind_all) + " (" + n + ")"
                : label + " (" + n + ")");
        chip.setChecked("".equals(label) ? cat.isEmpty() : label.equals(cat));
        chip.setOnCheckedChangeListener((c, checked) -> {
            if (checked) {
                cat = label;
                applyFilter();
            }
        });
        group.addView(chip);
    }

    /** Client-side category filter over the loaded entries; nothing invented. */
    private void applyFilter() {
        visible.clear();
        for (Api.Entry e : entries) {
            if (cat.isEmpty() || e.catLabel().equals(cat)) {
                visible.add(e);
            }
        }
        adapter.notifyDataSetChanged();
        TextView empty = findViewById(R.id.library_empty);
        empty.setVisibility(visible.isEmpty() && !entries.isEmpty() ? View.VISIBLE : View.GONE);
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

    private static String host(String url) {
        try {
            String h = Uri.parse(url).getHost();
            return h != null && h.startsWith("www.") ? h.substring(4) : (h == null ? "" : h);
        } catch (Exception e) {
            return "";
        }
    }

    private class Adapter extends BaseAdapter {
        @Override
        public int getCount() {
            return visible.size();
        }

        @Override
        public Api.Entry getItem(int position) {
            return visible.get(position);
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
            final View row = v;
            Api.Entry e = getItem(position);
            TextView kind = v.findViewById(R.id.entry_kind);
            TextView date = v.findViewById(R.id.entry_date);
            TextView title = v.findViewById(R.id.entry_title);
            TextView from = v.findViewById(R.id.entry_from);
            TextView summary = v.findViewById(R.id.entry_summary);
            LinearLayout tags = v.findViewById(R.id.entry_tags);
            Button copyPrompt = v.findViewById(R.id.entry_copy_prompt);
            Button copyInstall = v.findViewById(R.id.entry_copy_install);
            Button details = v.findViewById(R.id.entry_details);

            kind.setText("prompt".equals(e.kind)
                    ? R.string.entry_kind_prompt : R.string.entry_kind_link);
            date.setText(Ui.date(e.found));
            title.setText(e.title.isEmpty() ? (e.url.isEmpty() ? e.text : e.url) : e.title);
            String host = e.url.isEmpty() ? "" : host(e.url);
            from.setText(host);
            from.setVisibility(host.isEmpty() ? View.GONE : View.VISIBLE);
            String body = !e.summary.isEmpty() ? e.summary : (!e.desc.isEmpty() ? e.desc : e.text);
            summary.setText(body);
            summary.setVisibility(body.isEmpty() ? View.GONE : View.VISIBLE);

            tags.removeAllViews();
            LayoutInflater inflater = (LayoutInflater) getSystemService(Context.LAYOUT_INFLATER_SERVICE);
            for (int i = 0; i < e.tags.size() && i < MAX_TAGS; i++) {
                tags.addView(makeTag(inflater, e.tags.get(i)));
            }
            if (e.tags.size() > MAX_TAGS) {
                tags.addView(makeTag(inflater,
                        getString(R.string.tag_overflow, e.tags.size() - MAX_TAGS)));
            }
            tags.setVisibility(tags.getChildCount() == 0 ? View.GONE : View.VISIBLE);

            boolean hasPrompt = "prompt".equals(e.kind) && !e.text.isEmpty();
            copyPrompt.setVisibility(hasPrompt ? View.VISIBLE : View.GONE);
            if (hasPrompt) {
                copyPrompt.setOnClickListener(b ->
                        Ui.copy(LibraryActivity.this, row, "prompt", e.text));
            }
            copyInstall.setVisibility(e.install.isEmpty() ? View.GONE : View.VISIBLE);
            copyInstall.setOnClickListener(b ->
                    Ui.copy(LibraryActivity.this, row, "install", e.install));
            details.setOnClickListener(b -> {
                Intent i = new Intent(LibraryActivity.this, EntryActivity.class);
                i.putExtra("entryId", e.id);
                startActivity(i);
            });
            return v;
        }

        private TextView makeTag(LayoutInflater inflater, String text) {
            TextView tv = new TextView(LibraryActivity.this);
            tv.setText(text);
            tv.setBackgroundResource(R.drawable.tag_bg);
            tv.setTextAppearance(LibraryActivity.this, R.style.TextAppearance_Parcel_T1);
            tv.setTextColor(getColor(R.color.ink2));
            int h = (int) (8 * getResources().getDisplayMetrics().density);
            int vv = (int) (3 * getResources().getDisplayMetrics().density);
            tv.setPadding(h, vv, h, vv);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.setMarginEnd(h);
            tv.setLayoutParams(lp);
            return tv;
        }
    }
}
