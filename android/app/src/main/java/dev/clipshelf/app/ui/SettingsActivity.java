package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import android.app.AlertDialog;

import java.util.ArrayList;
import java.util.List;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;
import dev.clipshelf.app.net.Async.Done;
import dev.clipshelf.app.outbox.OutboxPolicy;

/**
 * Profile settings. Endpoint changes are verified against GET /api/me first:
 * a different instance or account is refused, because queued shares must
 * never be moved to another server or identity. Same-instance hostname
 * changes are routing-only and leave the outbox identity untouched.
 * Also discloses the Android background-delivery limits.
 */
public class SettingsActivity extends Activity {

    private Creds.Session session;
    private Creds.Profile profile;
    private List<Api.Collection> collections;
    private String selectedCollectionId;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        session = Creds.session(this);
        profile = Creds.profile(this);
        if (session == null || profile == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }

        TextView account = findViewById(R.id.settings_account);
        TextView instance = findViewById(R.id.settings_instance);
        account.setText(getString(R.string.signed_in_as, profile.email));
        instance.setText(getString(R.string.instance_label, profile.instanceId));

        // Default destination
        Spinner defaultCol = findViewById(R.id.settings_default_collection);
        Button applyDefault = findViewById(R.id.settings_default_apply);
        applyDefault.setOnClickListener(v -> {
            if (selectedCollectionId == null) {
                return;
            }
            applyDefault.setEnabled(false);
            Async.go(() -> Api.setDefaultCollection(session.profile.endpoint, session.token, selectedCollectionId),
                    (me, error) -> {
                        applyDefault.setEnabled(true);
                        if (error != null) {
                            Toast.makeText(this, Ui.message(this, error), Toast.LENGTH_LONG).show();
                            return;
                        }
                        String name = "";
                        for (Api.Collection c : me.collections) {
                            if (c.id.equals(me.defaultCollectionId)) {
                                name = c.name;
                                break;
                            }
                        }
                        Creds.updateDefaultCollection(this, me.defaultCollectionId, name);
                        Toast.makeText(this, R.string.collection_default_set, Toast.LENGTH_SHORT).show();
                    });
        });
        Async.go(() -> Api.me(session.profile.endpoint, session.token), (Done<Api.Me>) (me, error) -> {
            if (error != null) {
                Toast.makeText(this, Ui.message(this, error), Toast.LENGTH_LONG).show();
                return;
            }
            collections = me.collections;
            List<String> names = new ArrayList<>();
            int selected = 0;
            for (int i = 0; i < collections.size(); i++) {
                Api.Collection c = collections.get(i);
                names.add(c.name);
                if (c.id.equals(me.defaultCollectionId)) {
                    selected = i;
                }
            }
            ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                    android.R.layout.simple_spinner_dropdown_item, names);
            defaultCol.setAdapter(adapter);
            defaultCol.setSelection(selected);
            selectedCollectionId = collections.isEmpty() ? null : collections.get(selected).id;
            defaultCol.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
                @Override
                public void onItemSelected(AdapterView<?> p, View v, int pos, long id) {
                    if (pos >= 0 && pos < collections.size()) {
                        selectedCollectionId = collections.get(pos).id;
                    }
                }

                @Override
                public void onNothingSelected(AdapterView<?> p) {
                }
            });
        });

        // Endpoint update under the same instance/account only
        EditText endpoint = findViewById(R.id.settings_endpoint);
        endpoint.setText(profile.endpoint);
        Button update = findViewById(R.id.settings_endpoint_apply);
        update.setOnClickListener(v -> {
            final String candidate;
            try {
                candidate = Api.normalizeOrigin(endpoint.getText().toString());
            } catch (Exception e) {
                Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show();
                return;
            }
            update.setEnabled(false);
            Async.go(() -> Api.me(candidate, session.token), (me, error) -> {
                update.setEnabled(true);
                if (error != null) {
                    Toast.makeText(this, Ui.message(this, error), Toast.LENGTH_LONG).show();
                    return;
                }
                if (OutboxPolicy.identityMatches(me.instanceId, me.userId,
                        profile.instanceId, profile.userId)) {
                    Creds.updateEndpoint(this, candidate);
                    Toast.makeText(this, R.string.endpoint_same_instance_ok, Toast.LENGTH_LONG).show();
                } else {
                    // Different server/account: refuse, outbox identity must not move.
                    new AlertDialog.Builder(this)
                            .setTitle(R.string.error_title)
                            .setMessage(R.string.endpoint_mismatch_rejected)
                            .setPositiveButton(R.string.ok, null)
                            .show();
                }
            });
        });

        // Sign out
        Button logout = findViewById(R.id.settings_logout);
        logout.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setTitle(R.string.logout_confirm_title)
                .setMessage(R.string.logout_confirm_message)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    Async.go(() -> {
                        Api.logout(session.profile.endpoint, session.token);
                        return null;
                    }, (r, e) -> {
                        Creds.clear(this);
                        startActivity(new Intent(this, SetupActivity.class));
                        finish();
                    });
                })
                .setNegativeButton(R.string.cancel, null)
                .show());
    }
}
