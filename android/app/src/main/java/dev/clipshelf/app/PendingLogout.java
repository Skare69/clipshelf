package dev.clipshelf.app;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;

/**
 * Explicit offline sign-out state. When the server-side session revocation
 * cannot complete (offline, timeout, 5xx/429), the session token is re-stored
 * Keystore-encrypted in a prefs file separate from the profile — never
 * plaintext, and invisible to Creds.clear() — and revocation is retried from
 * MainActivity resume and the periodic DeliverWorker until the server
 * confirms. The local profile is always cleared immediately; this store only
 * exists to finish the server half of sign-out. A permanent server rejection
 * (other 4xx) is surfaced and nothing is retained.
 */
public final class PendingLogout {

    private static final String PREFS = "clipshelf.revoke";
    private static final String KEY = "pending";
    // ponytail: MAX_REVOKES_PER_CALL bounds one call to 3 revocations (~2 min
    // worst case at the 10s/30s Api timeouts) so dead endpoints cannot stall
    // outbox delivery for N × timeout. Records leave only on confirmation, so
    // unattempted records just wait for the next run (app open, drain,
    // periodic) — logout work is never dropped. Ceiling: a permanently dead
    // endpoint at the head delays later records until it leaves; rotate the
    // attempt window if that ever matters.
    /** Records a call may attempt; see MAX_REVOKES_PER_CALL. */
    private static final int MAX_REVOKES_PER_CALL = 3;
    private static final Object LOCK = new Object();

    private PendingLogout() {
    }

    /**
     * Revokes the server session. Returns null when revoked (or already gone:
     * 401), otherwise a user-facing notice — either pending-revocation kept
     * for automatic retry, or a permanent server rejection (nothing kept).
     */
    public static String signOut(Context ctx, String endpoint, String email, String token) {
        try {
            Api.logout(endpoint, token);
            return null;
        } catch (Api.ApiException e) {
            if (e.code >= 500 || e.code == 429) {
                return keep(ctx, endpoint, email, token);
            }
            return e.getMessage();
        } catch (IOException e) {
            return keep(ctx, endpoint, email, token);
        }
    }

    /**
     * Retries pending revocations, at most {@link #MAX_REVOKES_PER_CALL}
     * records per call. Confirmed records and records whose token can no
     * longer be decrypted (Keystore key lost; the session then expires
     * server-side on its own) are removed; the rest stay for the next call.
     * Best-effort and silent: callers show state via {@link #pendingLabels}.
     */
    public static void revokeAll(Context ctx) {
        synchronized (LOCK) {
            attemptBounded(records(ctx), PendingLogout::tryRevoke,
                    r -> remove(ctx, r), MAX_REVOKES_PER_CALL);
        }
    }

    /**
     * Attempts up to limit records in list order, removing each record the
     * attempt confirms. Returns the number of attempts made. Android-free core
     * so JVM tests can pin the bound; the confirmed sink must not structurally
     * modify rs.
     */
    static <T> int attemptBounded(List<T> rs, java.util.function.Predicate<T> attempt,
                                  java.util.function.Consumer<T> confirmed, int limit) {
        int attempts = 0;
        for (T r : rs) {
            if (attempts >= limit) {
                break;
            }
            attempts++;
            if (attempt.test(r)) {
                confirmed.accept(r);
            }
        }
        return attempts;
    }

    /** One pending revocation; true when the record is finished — confirmed, or
     *  permanently refused (the session then expires server-side on its own). */
    private static boolean tryRevoke(JSONObject r) {
        String token;
        try {
            token = Creds.decrypt(r.optString("token", ""));
        } catch (Exception e) {
            return true; // Keystore key lost; server-side expiry finishes the sign-out
        }
        try {
            Api.logout(r.optString("endpoint", ""), token);
            return true;
        } catch (Api.ApiException e) {
            return e.code < 500 && e.code != 429;
        } catch (IOException e) {
            return false; // transient: keep for the next attempt
        }
    }

    /** Account identities with an outstanding server-side sign-out (no crypto on the UI thread). */
    public static List<String> pendingLabels(Context ctx) {
        synchronized (LOCK) {
            List<String> out = new ArrayList<>();
            for (JSONObject r : records(ctx)) {
                out.add(r.optString("email", ""));
            }
            return out;
        }
    }

    private static String keep(Context ctx, String endpoint, String email, String token) {
        synchronized (LOCK) {
            try {
                List<JSONObject> rs = records(ctx);
                JSONObject r = new JSONObject();
                r.put("endpoint", endpoint);
                r.put("email", email);
                r.put("token", Creds.encrypt(token));
                rs.add(r);
                write(ctx, rs);
                return ctx.getString(R.string.logout_pending, email);
            } catch (Exception e) {
                // Keystore broken: cannot retain the token safely; say so instead of losing it silently.
                return ctx.getString(R.string.logout_pending_failed, email);
            }
        }
    }

    private static void remove(Context ctx, JSONObject r) {
        List<JSONObject> kept = new ArrayList<>();
        for (JSONObject other : records(ctx)) {
            if (!(r.optString("endpoint", "").equals(other.optString("endpoint", ""))
                    && r.optString("email", "").equals(other.optString("email", "")))) {
                kept.add(other);
            }
        }
        write(ctx, kept);
    }

    private static List<JSONObject> records(Context ctx) {
        List<JSONObject> out = new ArrayList<>();
        JSONArray a = parse(prefs(ctx).getString(KEY, "[]"));
        for (int i = 0; i < a.length(); i++) {
            JSONObject o = a.optJSONObject(i);
            if (o != null) {
                out.add(o);
            }
        }
        return out;
    }

    private static JSONArray parse(String s) {
        try {
            return new JSONArray(s);
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private static void write(Context ctx, List<JSONObject> rs) {
        JSONArray a = new JSONArray();
        for (JSONObject r : rs) {
            a.put(r);
        }
        prefs(ctx).edit().putString(KEY, a.toString()).apply();
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
