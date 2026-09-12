package dev.clipshelf.app.ui;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.view.View;
import android.widget.ImageView;
import android.widget.TextView;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;

import dev.clipshelf.app.Creds;
import dev.clipshelf.app.R;
import dev.clipshelf.app.net.Api;
import dev.clipshelf.app.net.Async;

/**
 * Server-authorized asset viewing with the session token. Images decode
 * downsampled into an ImageView; text/captions render inert (setText, no
 * HTML). Everything else is honestly declined instead of proxied.
 */
public class AssetActivity extends Activity {

    private static final int MAX_IMAGE_DIMENSION = 2048;
    private static final long TEXT_PREVIEW_BYTES = 256 * 1024;

    private Creds.Session session;
    private String url;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_asset);
        session = Creds.session(this);
        if (session == null) {
            Ui.sessionExpired(this);
            finish();
            return;
        }
        url = getIntent().getStringExtra("url");
        String name = getIntent().getStringExtra("name");
        String contentType = getIntent().getStringExtra("contentType");
        String kind = getIntent().getStringExtra("kind");
        setTitle(name.isEmpty() ? kind : name);

        boolean image = "image".equals(kind)
                || (contentType != null && contentType.startsWith("image/"));
        boolean text = "captions".equals(kind) || "page".equals(kind)
                || (contentType != null && (contentType.startsWith("text/")
                || contentType.contains("json")));
        if (image) {
            loadImage();
        } else if (text) {
            loadText();
        } else {
            decline();
        }
    }

    private void decline() {
        findViewById(R.id.asset_image).setVisibility(View.GONE);
        TextView status = findViewById(R.id.asset_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.asset_not_viewable);
    }

    private void loadImage() {
        TextView status = findViewById(R.id.asset_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.loading);
        Async.go(() -> {
            File dest = File.createTempFile("asset", ".bin", getCacheDir());
            Api.downloadAsset(session.profile.endpoint, session.token, url, dest, Api.MAX_ASSET_BYTES);
            return decode(dest);
        }, (bitmap, error) -> {
            if (error != null || bitmap == null) {
                status.setText(error == null ? getString(R.string.asset_load_failed, "?")
                        : getString(R.string.asset_load_failed, Ui.message(this, error)));
                return;
            }
            status.setVisibility(View.GONE);
            ImageView iv = findViewById(R.id.asset_image);
            iv.setVisibility(View.VISIBLE);
            iv.setImageBitmap(bitmap);
            iv.setContentDescription(getIntent().getStringExtra("name"));
        });
    }

    private Bitmap decode(File file) throws Exception {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        try (InputStream in = new FileInputStream(file)) {
            BitmapFactory.decodeStream(in, null, bounds);
        }
        int sample = 1;
        while (bounds.outWidth / sample > MAX_IMAGE_DIMENSION
                || bounds.outHeight / sample > MAX_IMAGE_DIMENSION) {
            sample *= 2;
        }
        BitmapFactory.Options opts = new BitmapFactory.Options();
        opts.inSampleSize = sample;
        try (InputStream in = new FileInputStream(file)) {
            Bitmap b = BitmapFactory.decodeStream(in, null, opts);
            if (b == null) {
                throw new Exception("Not a decodable image");
            }
            return b;
        } finally {
            // ponytail: no cache policy needed at one image per view; delete immediately.
            file.delete();
        }
    }

    private void loadText() {
        TextView status = findViewById(R.id.asset_status);
        status.setVisibility(View.VISIBLE);
        status.setText(R.string.loading);
        TextView body = findViewById(R.id.asset_body);
        Async.go(() -> {
            File dest = File.createTempFile("asset", ".txt", getCacheDir());
            Api.downloadAsset(session.profile.endpoint, session.token, url, dest, TEXT_PREVIEW_BYTES);
            byte[] all = new byte[(int) dest.length()];
            try (FileInputStream in = new FileInputStream(dest)) {
                int read = in.read(all);
                if (read < 0) {
                    read = 0;
                }
                return new String(all, 0, read, java.nio.charset.StandardCharsets.UTF_8);
            } finally {
                dest.delete();
            }
        }, (text, error) -> {
            if (error != null) {
                status.setText(getString(R.string.asset_load_failed, Ui.message(this, error)));
                return;
            }
            status.setVisibility(View.GONE);
            findViewById(R.id.asset_body_scroll).setVisibility(View.VISIBLE);
            body.setText(text == null ? "" : text); // inert text; never a WebView
        });
    }
}
