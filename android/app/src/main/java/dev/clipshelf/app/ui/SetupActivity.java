package dev.clipshelf.app.ui;

import android.app.Activity;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import java.io.IOException;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;
import dev.clipshelf.app.outbox.OutboxStore;
import dev.clipshelf.app.work.WorkScheduler;

/**
 * Server + account setup. Order matters: login -> GET /api/me pins instance,
 * account and default destination -> the durable periodic reconciliation is
 * registered BEFORE the app will accept any share -> paused outbox rows of
 * this identity resume. Password is used once and never stored.
 */
public class SetupActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_setup);

        EditText server = findViewById(R.id.setup_server);
        EditText email = findViewById(R.id.setup_email);
        EditText password = findViewById(R.id.setup_password);
        Button go = findViewById(R.id.setup_go);
        TextView status = findViewById(R.id.setup_status);

        Creds.Profile existing = Creds.profile(this);
        if (existing != null) {
            server.setText(existing.endpoint);
            email.setText(existing.email);
        }

        go.setOnClickListener(v -> {
            final String endpoint;
            try {
                endpoint = Api.normalizeOrigin(server.getText().toString());
            } catch (IOException e) {
                status.setText(e.getMessage());
                return;
            }
            final String mail = email.getText().toString().trim();
            final String pass = password.getText().toString();
            if (mail.isEmpty() || pass.isEmpty()) {
                status.setText(R.string.setup_missing_fields);
                return;
            }
            go.setEnabled(false);
            status.setText(R.string.signing_in);
            Async.go(() -> {
                String token = Api.login(endpoint, mail, pass);
                Api.Me me = Api.me(endpoint, token);
                String collectionName = "";
                if (me.defaultCollectionId != null) {
                    for (Api.Collection c : me.collections) {
                        if (me.defaultCollectionId.equals(c.id)) {
                            collectionName = c.name;
                            break;
                        }
                    }
                }
                Creds.save(this, new Creds.Profile(endpoint, me.instanceId, me.userId, me.email,
                        String.valueOf(me.isAppAdmin), me.defaultCollectionId, collectionName), token);
                // Durable periodic drain exists before any share is accepted.
                WorkScheduler.ensurePeriodic(this);
                // Re-login of this identity resumes its paused rows immediately.
                new OutboxStore(this).resetAuthPaused(me.instanceId, me.userId);
                WorkScheduler.drainNow(this);
                return null;
            }, (result, error) -> {
                go.setEnabled(true);
                if (error != null) {
                    status.setText(Ui.message(this, error));
                    password.setText("");
                    return;
                }
                setResult(RESULT_OK);
                finish();
            });
        });
    }
}
