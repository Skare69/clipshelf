package dev.clipshelf.app.ui;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
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
import dev.clipshelf.app.PendingLogout;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;
import dev.clipshelf.app.net.Async.Done;
import dev.clipshelf.app.outbox.OutboxPolicy;

/**
 * Profile settings. Endpoint changes are verified in two phases first: an
 * unauthenticated GET /api/instance must prove the candidate serves this
 * instance, then the user signs in fresh AT the candidate — the session
 * token never crosses to it, because the instance id is public and an
 * impostor host can copy it (review A2). A different instance or account is
 * refused, because queued shares must never be moved to another server or
 * identity. Same-instance hostname changes are routing-only and leave the
 * outbox identity untouched; the session left on the old origin is revoked.
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
        Async.goUi(this, () -> Api.me(session.profile.endpoint, session.token), (Done<Api.Me>) (me, error) -> {
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
            final String oldEndpoint = session.profile.endpoint;
            final String oldEmail = profile.email;
            final String oldToken = session.token;
            // The candidate is proven by a fresh sign-in there: the enrolled
            // session token never crosses to it (an impostor can copy the
            // public instance id, but not mint a session from a stolen one).
            final EditText password = new EditText(this);
            password.setInputType(InputType.TYPE_CLASS_TEXT
                    | InputType.TYPE_TEXT_VARIATION_PASSWORD);
            new AlertDialog.Builder(this)
                    .setTitle(R.string.endpoint_reauth_title)
                    .setMessage(getString(R.string.endpoint_reauth_message, candidate,
                            profile.email))
                    .setView(password)
                    .setPositiveButton(R.string.sign_in, (d, w) -> {
                        final String pass = password.getText().toString();
                        update.setEnabled(false);
                        Async.go(() -> Api.adoptCandidate(candidate, profile.instanceId,
                                profile.email, pass), (adopted, verifyError) -> {
                            update.setEnabled(true);
                            if (verifyError != null) {
                                if (verifyError instanceof Api.ApiException
                                        && ((Api.ApiException) verifyError).code == 403) {
                                    // Different server: refuse, outbox identity must not move.
                                    new AlertDialog.Builder(this)
                                            .setTitle(R.string.error_title)
                                            .setMessage(R.string.endpoint_mismatch_rejected)
                                            .setPositiveButton(R.string.ok, null)
                                            .show();
                                } else {
                                    Toast.makeText(this, Ui.message(this, verifyError), Toast.LENGTH_LONG).show();
                                }
                                return;
                            }
                            if (!OutboxPolicy.identityMatches(adopted.me.instanceId, adopted.me.userId,
                                    profile.instanceId, profile.userId)) {
                                // Different account on the same instance: refuse.
                                new AlertDialog.Builder(this)
                                        .setTitle(R.string.error_title)
                                        .setMessage(R.string.endpoint_mismatch_rejected)
                                        .setPositiveButton(R.string.ok, null)
                                        .show();
                                return;
                            }
                            // Persist the adopted session and rebuild the in-memory session
                            // and profile so later calls (default destination, sign-out)
                            // target the adopted origin and token, not a stale pair.
                            String collectionName = "";
                            if (adopted.me.defaultCollectionId != null) {
                                for (Api.Collection c : adopted.me.collections) {
                                    if (adopted.me.defaultCollectionId.equals(c.id)) {
                                        collectionName = c.name;
                                        break;
                                    }
                                }
                            }
                            Creds.save(this, new Creds.Profile(adopted.endpoint, adopted.me.instanceId,
                                    adopted.me.userId, adopted.me.email, adopted.me.defaultCollectionId,
                                    collectionName), adopted.token);
                            session = Creds.session(this);
                            profile = Creds.profile(this);
                            if (session == null || profile == null) {
                                Ui.sessionExpired(this);
                                finish();
                                return;
                            }
                            endpoint.setText(adopted.endpoint);
                            // Best effort: revoke the session left behind on the old origin.
                            Async.go(() -> PendingLogout.signOut(this, oldEndpoint, oldEmail, oldToken),
                                    (notice, e) -> {
                                        if (notice != null) {
                                            Toast.makeText(this, notice, Toast.LENGTH_LONG).show();
                                        }
                                    });
                            Toast.makeText(this, R.string.endpoint_same_instance_ok, Toast.LENGTH_LONG).show();
                        });
                    })
                    .setNegativeButton(R.string.cancel, null)
                    .show();
        });

        // Sign out
        Button logout = findViewById(R.id.settings_logout);
        logout.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setTitle(R.string.logout_confirm_title)
                .setMessage(R.string.logout_confirm_message)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    Async.go(() -> PendingLogout.signOut(this, session.profile.endpoint,
                            session.profile.email, session.token), (notice, e) -> {
                        Creds.clear(this);
                        if (notice != null) {
                            Toast.makeText(this, notice, Toast.LENGTH_LONG).show();
                        }
                        startActivity(new Intent(this, SetupActivity.class));
                        finish();
                    });
                })
                .setNegativeButton(R.string.cancel, null)
                .show());
    }
}
