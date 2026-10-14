package dev.clipshelf.app.ui;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.google.android.material.bottomnavigation.BottomNavigationView;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.snackbar.Snackbar;

import java.io.IOException;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Locale;

import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;

/** Small shared UI helpers. */
final class Ui {

    private Ui() {
    }

    /** 401/403 anywhere in the online screens: tell the user, offer re-login. */
    static void sessionExpired(Context context) {
        new MaterialAlertDialogBuilder(context)
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

    /** Copies and confirms with the contract's "Copied" snackbar. */
    static void copy(Context context, View anchor, String label, String text) {
        if (text == null || text.isEmpty()) {
            return;
        }
        Object systemService = context.getSystemService(Context.CLIPBOARD_SERVICE);
        ((ClipboardManager) systemService).setPrimaryClip(ClipData.newPlainText(label, text));
        Snackbar.make(anchor, R.string.copied, Snackbar.LENGTH_SHORT).show();
    }

    /** "Oct 13, 2026" from the API's ISO timestamp; falls back to the raw text. */
    static String date(String iso) {
        try {
            return DateTimeFormatter.ofPattern("MMM d, yyyy", Locale.getDefault())
                    .format(OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()));
        } catch (Exception e) {
            return iso == null ? "" : iso;
        }
    }

    /** "Oct 13, 14:05" from an epoch-millisecond timestamp. */
    static String time(long epochMs) {
        return DateTimeFormatter.ofPattern("MMM d, HH:mm", Locale.getDefault())
                .format(java.time.Instant.ofEpochMilli(epochMs).atZone(ZoneId.systemDefault()));
    }

    /**
     * Edge-to-edge insets, applied once per screen. The app bar container
     * pads its top so the label color runs under the status bar; the bottom
     * bar pads for the gesture bar (MDC does this itself); screens without
     * a bottom bar pad their bottom by the larger of navigation bar and
     * keyboard. Call after setContentView.
     */
    static void applyInsets(AppCompatActivity activity) {
        // The bars are label white in light mode: their icons must be dark.
        boolean night = (activity.getResources().getConfiguration().uiMode
                & android.content.res.Configuration.UI_MODE_NIGHT_MASK)
                == android.content.res.Configuration.UI_MODE_NIGHT_YES;
        androidx.core.view.WindowInsetsControllerCompat controller = androidx.core.view.WindowCompat
                .getInsetsController(activity.getWindow(), activity.getWindow().getDecorView());
        controller.setAppearanceLightStatusBars(!night);
        controller.setAppearanceLightNavigationBars(!night);
        ViewGroup root = (ViewGroup) activity.findViewById(android.R.id.content);
        if (root == null || root.getChildCount() == 0) {
            return;
        }
        View bar = activity.findViewById(R.id.appbar_container);
        View nav = activity.findViewById(R.id.bottom_nav);
        View content = root.getChildAt(0);
        ViewCompat.setOnApplyWindowInsetsListener(content, (v, insets) -> {
            Insets bars = insets.getInsets(
                    WindowInsetsCompat.Type.systemBars()
                            | WindowInsetsCompat.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            if (bar != null) {
                bar.setPadding(0, bars.top, 0, 0);
            }
            if (nav == null) {
                content.setPadding(0, 0, 0, Math.max(bars.bottom, ime.bottom));
            }
            return insets;
        });
    }

    /** Up arrow on a child screen's toolbar; system Back always works. */
    static void up(AppCompatActivity activity) {
        androidx.appcompat.widget.Toolbar toolbar = activity.findViewById(R.id.toolbar);
        if (toolbar == null) {
            return;
        }
        toolbar.setNavigationIcon(androidx.appcompat.R.drawable.abc_ic_ab_back_material);
        ((com.google.android.material.appbar.MaterialToolbar) toolbar)
                .setNavigationIconTint(activity.getColor(R.color.ink));
        toolbar.setNavigationOnClickListener(
                v -> activity.getOnBackPressedDispatcher().onBackPressed());
    }

    /**
     * Wires the four top-level destinations. The current screen shows as
     * selected; Back from any destination returns to Library, then exits.
     */
    static void bottomNav(AppCompatActivity activity) {
        BottomNavigationView nav = activity.findViewById(R.id.bottom_nav);
        if (nav == null) {
            return;
        }
        nav.inflateMenu(R.menu.bottom_nav);
        // Select before the listener exists, so the selection does not navigate.
        nav.setSelectedItemId(activity instanceof OutboxActivity ? R.id.nav_outbox
                : activity instanceof CollectionsActivity ? R.id.nav_collections
                : activity instanceof SettingsActivity ? R.id.nav_settings
                : R.id.nav_library);
        nav.setOnItemSelectedListener(item -> {
            Class<?> target;
            int id = item.getItemId();
            if (id == R.id.nav_library) {
                target = LibraryActivity.class;
            } else if (id == R.id.nav_outbox) {
                target = OutboxActivity.class;
            } else if (id == R.id.nav_collections) {
                target = CollectionsActivity.class;
            } else if (id == R.id.nav_settings) {
                target = SettingsActivity.class;
            } else {
                return false;
            }
            if (target == activity.getClass()) {
                return true;
            }
            Intent intent = new Intent(activity, target).addFlags(Intent.FLAG_ACTIVITY_NO_ANIMATION);
            if (target == LibraryActivity.class) {
                intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            }
            activity.startActivity(intent);
            if (!(activity instanceof LibraryActivity)) {
                activity.finish();
                activity.overridePendingTransition(0, 0);
            }
            // This screen stays in the back stack (Library) or closes: either
            // way its own item must stay the selected one.
            return false;
        });
    }

    /** A tag chip: T1 text on the tag background, 8/3dp padding, 8dp end margin. */
    static TextView tag(Context c, String text) {
        TextView tv = new TextView(c);
        tv.setText(text);
        tv.setBackgroundResource(R.drawable.tag_bg);
        tv.setTextAppearance(c, R.style.TextAppearance_Parcel_T1);
        tv.setTextColor(c.getColor(R.color.ink2));
        int h = Math.round(8 * c.getResources().getDisplayMetrics().density);
        int v = Math.round(3 * c.getResources().getDisplayMetrics().density);
        tv.setPadding(h, v, h, v);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMarginEnd(h);
        tv.setLayoutParams(lp);
        return tv;
    }
}
