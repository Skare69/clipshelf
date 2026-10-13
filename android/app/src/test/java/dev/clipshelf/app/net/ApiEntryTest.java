package dev.clipshelf.app.net;

import org.json.JSONObject;
import org.junit.Test;

import java.util.Arrays;
import java.util.Collections;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

/**
 * The entry payload as serialize_entry sends it: "cat" is a list, sources and
 * contributors are objects, "found" is an ISO time. The library labels show
 * these values, so a parse that drops them shows wrong or raw data.
 */
public class ApiEntryTest {

    @Test
    public void parsesServerShapes() throws Exception {
        Api.Entry e = new Api.Entry(new JSONObject("{"
                + "\"id\":\"e1\",\"kind\":\"link\",\"url\":\"https://github.com/astral-sh/uv\","
                + "\"title\":\"astral-sh/uv\",\"summary\":\"Fast package manager.\","
                + "\"cat\":[\"Python\",\"Tools\"],\"tags\":[\"python\",\"rust\"],"
                + "\"sources\":[{\"url\":\"https://docs.astral.sh/uv/\",\"title\":\"Docs\"},"
                + "{\"url\":\"https://pypi.org/project/uv/\",\"title\":\"\"},{\"url\":\"\"}],"
                + "\"contributors\":[{\"id\":\"u1\",\"email\":\"dana@example.com\"},{\"id\":\"u2\"}],"
                + "\"found\":\"2026-10-11T21:06:00+00:00\",\"interpreted\":true,\"can_remove\":true}"));
        assertEquals("Python", e.catLabel());
        assertEquals(Arrays.asList("Python", "Tools"), e.cats);
        assertEquals("Fast package manager.", e.summary);
        assertEquals("2026-10-11T21:06:00+00:00", e.found);
        assertEquals(Arrays.asList("Docs — https://docs.astral.sh/uv/", "https://pypi.org/project/uv/"), e.sources);
        assertEquals(Collections.singletonList("dana@example.com"), e.contributors);
        assertTrue(e.interpreted);
    }

    @Test
    public void uninterpretedEntryFallsBackToOther() throws Exception {
        Api.Entry e = new Api.Entry(new JSONObject("{\"id\":\"e2\",\"kind\":\"link\",\"cat\":[]}"));
        assertEquals("Other", e.catLabel());
        assertTrue(e.sources.isEmpty());
        assertTrue(e.contributors.isEmpty());
    }
}
