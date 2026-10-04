package dev.clipshelf.app.net;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import dev.clipshelf.app.BuildConfig;

/**
 * Minimal HTTPS JSON client over HttpURLConnection. Bounded timeouts, bounded
 * response sizes, redirect following only for safe GET requests and only over
 * an allowed scheme (never an https downgrade; loopback http is debug-only).
 * All calls must run off the main thread.
 */
public final class Api {

    public static final int MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
    public static final int MAX_ASSET_BYTES = 64 * 1024 * 1024;
    private static final int CONNECT_TIMEOUT_MS = 10_000;
    private static final int READ_TIMEOUT_MS = 30_000;

    private Api() {
    }

    public static final class ApiException extends IOException {
        public final int code;

        ApiException(int code, String message) {
            super(message);
            this.code = code;
        }
    }

    // ---- Parsed payloads ----

    public static final class Collection {
        public final String id, name, kind, ownerId;
        public final boolean canManage;

        Collection(JSONObject o) {
            id = o.optString("id");
            name = o.optString("name");
            kind = o.optString("kind");
            ownerId = o.optString("owner_id");
            canManage = o.optBoolean("can_manage");
        }
    }


    public static final class Me {
        public final String instanceId, userId, email, defaultCollectionId;
        public final List<Collection> collections;

        Me(JSONObject o) {
            instanceId = o.optString("instance_id");
            JSONObject u = o.optJSONObject("user");
            userId = u == null ? "" : u.optString("id");
            email = u == null ? "" : u.optString("email");
            defaultCollectionId = o.optString("default_collection_id", null);
            collections = new ArrayList<>();
            JSONArray cs = o.optJSONArray("collections");
            if (cs != null) {
                for (int i = 0; i < cs.length(); i++) {
                    collections.add(new Collection(cs.optJSONObject(i)));
                }
            }
        }
    }

    public static final class Member {
        public final String id, email;
        public final boolean isOwner;

        Member(JSONObject o) {
            id = o.optString("id");
            email = o.optString("email");
            isOwner = o.optBoolean("is_owner");
        }
    }

    public static final class Asset {
        public final String id, url, kind, contentType, name;

        Asset(JSONObject o) {
            id = o.optString("id");
            url = o.optString("url");
            kind = o.optString("kind");
            contentType = o.optString("content_type");
            name = o.optString("name");
        }
    }

    public static final class Job {
        public final String id, url, state, acquisition, interpretation, error;
        public final int attempts;
        public final boolean guardrail;
        public final int screeningWarnings;

        Job(JSONObject o) {
            id = o.optString("id");
            url = o.optString("url");
            state = o.optString("state");
            acquisition = o.optString("acquisition");
            interpretation = o.optString("interpretation");
            error = o.optString("error", "");
            attempts = o.optInt("attempts");
            guardrail = o.optBoolean("guardrail", false);
            JSONArray sw = o.optJSONArray("screening_warnings");
            screeningWarnings = sw == null ? 0 : sw.length();
        }
    }

    public static final class Entry {
        public final String id, kind, url, title, desc, text, cat;
        public final List<String> tags, sources, contributors;
        public final String install;
        public final boolean found, interpreted, canRemove;

        Entry(JSONObject o) {
            id = o.optString("id");
            kind = o.optString("kind");
            url = o.optString("url", "");
            title = o.optString("title", "");
            desc = o.optString("desc", "");
            text = o.optString("text", "");
            cat = o.optString("cat", "");
            install = o.optString("install", "");
            found = o.optBoolean("found");
            interpreted = o.optBoolean("interpreted");
            canRemove = o.optBoolean("can_remove");
            tags = strings(o, "tags");
            sources = strings(o, "sources");
            contributors = strings(o, "contributors");
        }
    }

    public static final class EntryDetail {
        public final Entry entry;
        public final List<Asset> assets;
        public final List<Job> jobs;

        EntryDetail(JSONObject o) {
            entry = new Entry(o.optJSONObject("entry"));
            assets = new ArrayList<>();
            JSONArray a = o.optJSONArray("assets");
            if (a != null) {
                for (int i = 0; i < a.length(); i++) {
                    assets.add(new Asset(a.optJSONObject(i)));
                }
            }
            jobs = new ArrayList<>();
            JSONArray j = o.optJSONArray("jobs");
            if (j != null) {
                for (int i = 0; i < j.length(); i++) {
                    jobs.add(new Job(j.optJSONObject(i)));
                }
            }
        }
    }

    public static final class Receipt {
        public final String id, clientRequestId, instanceId, userId, requestedCollectionId, collectionId, notice, json;

        Receipt(JSONObject r, String raw) {
            id = r.optString("id");
            clientRequestId = r.optString("client_request_id");
            instanceId = r.optString("instance_id");
            userId = r.optString("user_id");
            requestedCollectionId = r.optString("requested_collection_id", null);
            collectionId = r.optString("collection_id");
            notice = r.optString("notice", "");
            json = raw;
        }
    }

    public static final class EntriesPage {
        public final List<Entry> entries;
        public final int count;

        EntriesPage(JSONObject o) {
            count = o.optInt("count");
            entries = new ArrayList<>();
            JSONArray a = o.optJSONArray("entries");
            if (a != null) {
                for (int i = 0; i < a.length(); i++) {
                    entries.add(new Entry(a.optJSONObject(i)));
                }
            }
        }
    }

    // ---- Endpoints ----

    /** Builds a request body, converting org.json's checked JSONException into IOException. */
    private static JSONObject body(BodyBuilder build) throws IOException {
        try {
            JSONObject o = new JSONObject();
            build.build(o);
            return o;
        } catch (org.json.JSONException e) {
            throw new IOException(e);
        }
    }

    private interface BodyBuilder {
        void build(JSONObject o) throws org.json.JSONException;
    }

    /** allauth headless login; returns the session token (meta.session_token). */
    public static String login(String endpoint, String email, String password) throws IOException {
        JSONObject body = body(o -> {
            o.put("email", email);
            o.put("password", password);
        });
        JSONObject resp = requestJson(endpoint + "/_allauth/app/v1/auth/login", "POST", null, body);
        String token = resp.optJSONObject("meta") == null ? null
                : resp.optJSONObject("meta").optString("session_token", null);
        if (token == null || token.isEmpty()) {
            throw new ApiException(resp.optInt("status", 400), firstError(resp, "Login failed."));
        }
        return token;
    }

    /** Allauth session deletion. 401 counts as already signed out; other failures throw. */
    public static void logout(String endpoint, String token) throws IOException {
        try {
            requestJson(endpoint + "/_allauth/app/v1/auth/session", "DELETE", token, null);
        } catch (ApiException e) {
            if (e.code != 401) {
                throw e;
            }
        }
    }

    public static Me me(String endpoint, String token) throws IOException {
        return new Me(requestJson(endpoint + "/api/me", "GET", token, null));
    }

    /**
     * Unauthenticated probe for a candidate origin's instance id. Endpoint
     * changes use it to verify the candidate serves the enrolled instance
     * BEFORE any session token is sent there. Takes no token by design.
     * Servers older than this release answer 404 (endpoint change fails closed).
     */
    public static String instanceId(String endpoint) throws IOException {
        return requestJson(endpoint + "/api/instance", "GET", null, null).optString("instance_id", "");
    }

    /** Result of adopting a candidate endpoint for the enrolled instance. */
    public static final class Adopted {
        public final String endpoint, token;
        public final Me me;

        Adopted(String endpoint, String token, Me me) {
            this.endpoint = endpoint;
            this.token = token;
            this.me = me;
        }
    }

    /**
     * Proves a candidate endpoint serves the enrolled instance, then adopts
     * it with a fresh login AT the candidate. The instance id is public
     * (unauthenticated /api/instance), so an impostor host can copy it: the
     * enrolled session token therefore never crosses to the candidate — the
     * account is proven by re-entering the password there instead. Mismatch
     * throws ApiException(403) before any credential leaves the phone.
     */
    public static Adopted adoptCandidate(String candidate, String expectedInstanceId,
                                         String email, String password) throws IOException {
        String instanceId = instanceId(candidate);
        if (!expectedInstanceId.equals(instanceId)) {
            throw new ApiException(403, "candidate serves a different instance");
        }
        String token = login(candidate, email, password);
        return new Adopted(candidate, token, me(candidate, token));
    }

    public static List<Collection> collections(String endpoint, String token) throws IOException {
        JSONArray a = requestJson(endpoint + "/api/collections", "GET", token, null).optJSONArray("collections");
        List<Collection> out = new ArrayList<>();
        if (a != null) {
            for (int i = 0; i < a.length(); i++) {
                out.add(new Collection(a.optJSONObject(i)));
            }
        }
        return out;
    }

    public static Collection createCollection(String endpoint, String token, String name) throws IOException {
        JSONObject body = body(o -> o.put("name", name));
        return new Collection(requestJson(endpoint + "/api/collections", "POST", token, body).optJSONObject("collection"));
    }

    public static List<Member> members(String endpoint, String token, String collectionId) throws IOException {
        JSONArray a = requestJson(endpoint + "/api/collections/" + collectionId, "GET", token, null)
                .optJSONArray("members");
        List<Member> out = new ArrayList<>();
        if (a != null) {
            for (int i = 0; i < a.length(); i++) {
                out.add(new Member(a.optJSONObject(i)));
            }
        }
        return out;
    }

    public static void addMember(String endpoint, String token, String collectionId, String email) throws IOException {
        requestJson(endpoint + "/api/collections/" + collectionId + "/members", "POST", token,
                body(o -> o.put("email", email)));
    }

    public static void removeMember(String endpoint, String token, String collectionId, String userId) throws IOException {
        requestJson(endpoint + "/api/collections/" + collectionId + "/members/" + userId, "DELETE", token, null);
    }

    /** POST /api/settings {default_collection_id} answers with the fresh me payload. */
    public static Me setDefaultCollection(String endpoint, String token, String collectionId) throws IOException {
        JSONObject resp = requestJson(endpoint + "/api/settings", "POST", token,
                body(o -> o.put("default_collection_id", collectionId)));
        return new Me(resp);
    }

    public static EntriesPage entries(String endpoint, String token, String collectionId, String q,
                                      String sort, String kind, int offset, int limit) throws IOException {
        StringBuilder url = new StringBuilder(endpoint).append("/api/entries?collection_id=").append(qEnc(collectionId));
        if (q != null && !q.isEmpty()) {
            url.append("&q=").append(qEnc(q));
        }
        if (kind != null && !kind.isEmpty()) {
            url.append("&kind=").append(qEnc(kind));
        }
        url.append("&sort=").append(qEnc(sort == null || sort.isEmpty() ? "new" : sort));
        url.append("&offset=").append(offset).append("&limit=").append(limit);
        return new EntriesPage(requestJson(url.toString(), "GET", token, null));
    }

    public static EntryDetail entry(String endpoint, String token, String entryId) throws IOException {
        return new EntryDetail(requestJson(endpoint + "/api/entries/" + entryId, "GET", token, null));
    }

    public static void deleteEntry(String endpoint, String token, String entryId) throws IOException {
        requestJson(endpoint + "/api/entries/" + entryId, "DELETE", token, null);
    }

    public static Receipt capture(String endpoint, String token, String clientRequestId, String text,
                                  String collectionId, String instanceId, String userId) throws IOException {
        JSONObject payload = body(o -> {
            o.put("client_request_id", clientRequestId);
            o.put("text", text);
            o.put("collection_id", collectionId == null ? JSONObject.NULL : collectionId);
            o.put("instance_id", instanceId);
            o.put("user_id", userId);
        });
        JSONObject resp = requestJson(endpoint + "/api/captures", "POST", token, payload);
        JSONObject r = resp.optJSONObject("receipt");
        if (r == null) {
            throw new IOException("Malformed receipt from server");
        }
        return new Receipt(r, r.toString());
    }
    public static void retryJob(String endpoint, String token, String jobId) throws IOException {
        requestJson(endpoint + "/api/jobs/" + jobId + "/retry", "POST", token, new JSONObject());
    }

    /**
     * Authorized asset download to a cache file. Returns content type.
     * The server enforces membership + remaining-contribution authorization.
     */
    public static String downloadAsset(String endpoint, String token, String assetUrlOrPath,
                                       File dest, long maxBytes) throws IOException {

        if (maxBytes < 0) {
            throw new IOException("Asset too large: " + maxBytes + " bytes");
        }
        String url = assetUrlOrPath.startsWith("http") ? assetUrlOrPath : endpoint + assetUrlOrPath;
        // Finding A2 containment: the session token never leaves the origin it
        // was issued for; server-provided absolute asset URLs on other origins
        // load without it (public assets render, authorized ones fail closed).
        HttpURLConnection c = openWithRedirects(url, "GET", sameOrigin(url, endpoint) ? token : null, null);
        try {
            int code = c.getResponseCode();
            if (code != 200) {
                throw new ApiException(code, errorMessage(code, readAtMost(c.getErrorStream(), MAX_RESPONSE_BYTES)));
            }
            String contentType = c.getContentType();
            long len = c.getContentLengthLong();
            if (len > maxBytes) {
                throw new IOException("Asset too large: " + len + " bytes");
            }
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(dest)) {
                byte[] buf = new byte[16 * 1024];
                long total = 0;
                int n;
                while (total < maxBytes
                        && (n = in.read(buf, 0, (int) Math.min(buf.length, maxBytes - total))) != -1) {
                    out.write(buf, 0, n);
                    total += n;
                }
                if (total == maxBytes && in.read() != -1) {
                    throw new IOException("Response exceeds size limit");
                }
            }
            return contentType;
        } finally {
            c.disconnect();
        }
    }

    // ---- Plumbing ----

    private static JSONObject requestJson(String url, String method, String token, JSONObject body) throws IOException {
        HttpURLConnection c = openWithRedirects(url, method, token, body);
        try {
            int code = c.getResponseCode();
            byte[] raw = code >= 400 ? readAtMost(c.getErrorStream(), MAX_RESPONSE_BYTES)
                    : readAtMost(c.getInputStream(), MAX_RESPONSE_BYTES);
            String text = new String(raw, StandardCharsets.UTF_8);
            if (code >= 400 && !text.trim().startsWith("{")) {
                throw new ApiException(code, errorMessage(code, new byte[0]));
            }
            JSONObject json;
            try {
                json = text.isEmpty() ? new JSONObject() : new JSONObject(text);
            } catch (Exception e) {
                if (code >= 400) {
                    throw new ApiException(code, errorMessage(code, new byte[0]));
                }
                throw new IOException("Malformed response from server");
            }
            if (code >= 400) {
                throw new ApiException(code, firstError(json, errorMessage(code, text.getBytes(StandardCharsets.UTF_8))));
            }
            return json;
        } finally {
            c.disconnect();
        }
    }

    private static HttpURLConnection openWithRedirects(String url, String method, String token, JSONObject body)
            throws IOException {
        boolean safe = "GET".equals(method);
        String origin = originOf(url); // the origin the token may travel to
        for (int hop = 0; hop < 4; hop++) {
            checkScheme(url);
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(CONNECT_TIMEOUT_MS);
            c.setReadTimeout(READ_TIMEOUT_MS);
            c.setInstanceFollowRedirects(false);
            c.setRequestProperty("Accept", "application/json");
            if (token != null) {
                c.setRequestProperty("X-Session-Token", token);
            }
            c.setRequestMethod(method);
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                c.setDoOutput(true);
                c.setFixedLengthStreamingMode(bytes.length);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                try (OutputStream out = c.getOutputStream()) {
                    out.write(bytes);
                }
            }
            if (!safe) {
                return c; // never follow redirects for mutations (would forward the session token)
            }
            int code = c.getResponseCode();
            if (code >= 300 && code < 400) {
                String loc = c.getHeaderField("Location");
                c.disconnect();
                if (loc == null) {
                    throw new IOException("Redirect without Location");
                }
                url = new URL(new URL(url), loc).toString();
                if (token != null && !originOf(url).equals(origin)) {
                    throw new IOException("Refusing cross-origin redirect on an authenticated request");
                }
                continue; // re-checked by checkScheme: blocks https -> http downgrade
            }
            return c;
        }
        throw new IOException("Too many redirects");
    }

    /** scheme + host + effective port; the session token never crosses this boundary. */
    private static String originOf(String url) throws IOException {
        try {
            URL u = new URL(url);
            int port = u.getPort();
            if (port == -1) {
                port = "https".equalsIgnoreCase(u.getProtocol()) ? 443 : 80;
            }
            return u.getProtocol().toLowerCase(java.util.Locale.ROOT) + "://"
                    + u.getHost().toLowerCase(java.util.Locale.ROOT) + ":" + port;
        } catch (IOException e) {
            throw e;
        } catch (Exception e) {
            throw new IOException("Invalid server address");
        }
    }

    private static boolean sameOrigin(String a, String b) {
        try {
            return originOf(a).equals(originOf(b));
        } catch (IOException e) {
            return false;
        }
    }

    /** HTTPS-only in release. Debug builds may use plain http to emulator loopback only. */
    private static void checkScheme(String url) throws IOException {
        String scheme = null, host = null;
        try {
            URL u = new URL(url);
            scheme = u.getProtocol();
            host = u.getHost();
        } catch (Exception e) {
            throw new IOException("Invalid server address");
        }
        boolean debugLoopback = BuildConfig.DEBUG && "http".equalsIgnoreCase(scheme)
                && ("localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "10.0.2.2".equals(host));
        if (!"https".equalsIgnoreCase(scheme) && !debugLoopback) {
            throw new IOException("Only https:// server addresses are allowed");
        }
    }

    /** Normalizes a user-typed origin: trims, defaults the scheme to https, strips trailing slashes. */
    public static String normalizeOrigin(String raw) throws IOException {
        String u = raw == null ? "" : raw.trim();
        if (u.isEmpty()) {
            throw new IOException("Enter the server address");
        }
        if (!u.matches("(?i)^[a-z][a-z0-9+.-]*://.*")) {
            u = "https://" + u;
        }
        while (u.endsWith("/")) {
            u = u.substring(0, u.length() - 1);
        }
        return u;
    }


    private static byte[] readAtMost(InputStream in, int maxBytes) throws IOException {
        if (in == null) {
            return new byte[0];
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[16 * 1024];
        int n;
        int total = 0;
        while ((n = in.read(buf)) != -1) {
            total += n;
            if (total > maxBytes) {
                throw new IOException("Response exceeds size limit");
            }
            out.write(buf, 0, n);
        }
        in.close();
        return out.toByteArray();
    }

    private static String errorMessage(int code, byte[] body) {
        String detail = new String(body, StandardCharsets.UTF_8);
        if (detail.length() > 300) {
            detail = detail.substring(0, 300) + "…";
        }
        return "Server error " + code + (detail.isEmpty() ? "" : ": " + detail);
    }

    /** Reads "detail" (DRF) or the first message from allauth's errors list. */
    private static String firstError(JSONObject resp, String fallback) {
        String detail = resp.optString("detail", null);
        if (detail != null && !detail.isEmpty()) {
            return detail;
        }
        JSONArray errors = resp.optJSONArray("errors");
        if (errors != null && errors.length() > 0) {
            JSONObject e = errors.optJSONObject(0);
            if (e != null) {
                String msg = e.optString("message", null);
                if (msg != null && !msg.isEmpty()) {
                    return msg;
                }
            }
        }
        return fallback;
    }

    private static List<String> strings(JSONObject o, String key) {
        List<String> out = new ArrayList<>();
        JSONArray a = o.optJSONArray(key);
        if (a != null) {
            for (int i = 0; i < a.length(); i++) {
                String s = a.optString(i, null);
                if (s != null && !s.isEmpty()) {
                    out.add(s);
                }
            }
        }
        return out;
    }

    private static String qEnc(String s) {
        try {
            return java.net.URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }
}
