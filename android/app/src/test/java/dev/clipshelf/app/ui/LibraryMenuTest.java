package dev.clipshelf.app.ui;

import static org.junit.Assert.assertEquals;

import android.content.Context;

import androidx.appcompat.widget.Toolbar;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import java.security.Provider;
import java.security.Security;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.PendingLogoutRetryTest;
import dev.clipshelf.app.R;

/**
 * The library toolbar must hold exactly one copy of each menu_library item.
 * A second inflateMenu appends duplicates: the overflow then shows every
 * item twice, and only the first copies follow a sort/kind tap.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 34)
public class LibraryMenuTest {

    @Before
    public void setUp() {
        // Robolectric has no AndroidKeyStore: reuse the fixed-key stand-in so
        // Creds.save/session round-trip through the real AES-GCM path.
        Security.addProvider(new Provider("FakeAndroidKeyStore", 1.0, "test-only AndroidKeyStore") {
            {
                putService(new Service(this, "KeyStore", "AndroidKeyStore",
                        PendingLogoutRetryTest.FixedKeyStore.class.getName(), null, null) {
                    @Override
                    public Object newInstance(Object param) {
                        return new PendingLogoutRetryTest.FixedKeyStore();
                    }
                });
            }
        });
        Context ctx = RuntimeEnvironment.getApplication();
        Creds.save(ctx, new Creds.Profile("https://e.example", "inst-1", "user-1", "a@b.c",
                null, null), "tok");
    }

    @Test
    public void menuHoldsExactlyOneCopyOfEachLibraryItem() {
        LibraryActivity activity = Robolectric.buildActivity(LibraryActivity.class).setup().get();
        Toolbar toolbar = activity.findViewById(R.id.toolbar);
        assertEquals(3, toolbar.getMenu().size());
    }
}
