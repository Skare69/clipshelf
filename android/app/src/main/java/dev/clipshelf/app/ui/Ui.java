package dev.clipshelf.app.ui;

import android.content.Context;
import android.content.Intent;

import android.app.AlertDialog;

import java.io.IOException;

import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;

/** Small shared UI helpers. */
final class Ui {

    private Ui() {
    }

    /** 401/403 anywhere in the online screens: tell the user, offer re-login. */
    static void sessionExpired(Context context) {
        new AlertDialog.Builder(context)
                .setTitle(R.string.error_title)
                .setMessage(R.string.session_expired)
                .setPositiveButton(R.string.ok, (d, w) ->
                        context.startActivity(new Intent(context, SetupActivity.class)))
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    /** Human message for an async failure; auth failures route to re-login. */
    static String message(Context context, Exception e) {
        if (e instanceof Api.ApiException) {
            int code = ((Api.ApiException) e).code;
            if (code == 401 || code == 403) {
                sessionExpired(context);
                return context.getString(R.string.session_expired);
            }
            return e.getMessage();
        }
        if (e instanceof IOException) {
            return context.getString(R.string.offline_message, e.getMessage());
        }
        return String.valueOf(e.getMessage());
    }
}
