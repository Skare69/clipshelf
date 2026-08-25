"""Self-check: python test_clipshelf.py"""
import base64
import json
import tempfile
from pathlib import Path

import clipshelf.lib as cs

cs.CACHE = Path(tempfile.mkdtemp())  # keep test artifacts out of the repo cache/


def test():
    # norm: canonicalize + dedup
    assert cs.norm("https://GitHub.com/a/b/?utm_source=x#frag") == cs.norm("https://github.com/a/b")
    assert cs.norm("https://github.com/Foo/Bar") == cs.norm("https://github.com/foo/bar")
    assert cs.norm("https://Example.com/CaseMatters") != cs.norm("https://example.com/casematters")
    assert cs.repo_url("Foo/Bar") == "https://github.com/foo/bar"
    assert cs.repo_url("github.com/Foo/Bar") == "https://github.com/foo/bar"
    assert cs.repo_url("https://github.com/Foo/Bar") == "https://github.com/foo/bar"
    assert cs.norm("ftp://x") is None and cs.norm("not a url") is None

    # tagging + terminal hosts
    assert cs.tag_for("https://github.com/foo/bar") == "github"
    assert cs.tag_for("https://gist.github.com/x/1") == "github"
    assert cs.tag_for("https://huggingface.co/x") == "huggingface"
    assert cs.tag_for("https://example.com/x", "Awesome Prompt list") == "prompt"
    assert cs.tag_for("https://example.com/boring") is None
    # github nav chrome / non-repo shapes are rejected
    for noise in ("https://github.com", "https://github.com/anthropics",
                  "https://github.com/features/copilot", "https://github.com/login",
                  "https://github.com/a/b/blob/main/x.ipynb", "https://docs.github.com"):
        assert cs.tag_for(noise) is None, noise
    assert cs.is_terminal("https://github.com/a/b") and not cs.is_terminal("https://example.com")

    # shortener detection
    for short in ("https://share.google/abc", "https://vm.tiktok.com/ZS8x",
                  "https://www.reddit.com/r/x/s/TOKEN", "https://www.tiktok.com/t/ZTx"):
        assert cs.is_short(short), short
    for full in ("https://www.tiktok.com/@u/video/1", "https://reddit.com/r/x/comments/1/y",
                 "https://github.com/a/b"):
        assert not cs.is_short(full), full

    # triage: extracts, dedups, groups by host, splits seen/new (no shorteners -> no net)
    tlib = {"seen": {"https://github.com/a/b": "2026-01-01"}, "links": {}, "prompts": {}}
    groups = cs.triage("see https://github.com/a/b and https://GitHub.com/a/b/ plus\n"
                       "https://www.tiktok.com/@u/video/9 also https://github.com/c/d", tlib)
    assert set(groups) == {"github.com", "tiktok.com"}
    assert groups["github.com"] == [("https://github.com/a/b", True),
                                    ("https://github.com/c/d", False)]
    assert groups["tiktok.com"] == [("https://www.tiktok.com/@u/video/9", False)]

    # split_dump: tiktok -> panel, fresh -> scan queue, seen -> known
    tiktok, fresh, known = cs.split_dump(
        "https://github.com/a/b https://github.com/c/d "
        "https://www.tiktok.com/@u/video/9 https://example.com/guide", tlib)
    assert tiktok == ["https://www.tiktok.com/@u/video/9"]
    assert set(fresh) == {"https://github.com/c/d", "https://example.com/guide"}
    assert known == 1

    # parser: title, meta desc, links with anchor text
    p = cs.PageParser()
    p.feed('<title>T</title><meta name="description" content="D">'
           '<a href="/rel">Awesome  prompts</a><a href="https://github.com/a/b">repo</a>')
    assert p.title == "T" and p.desc == "D"
    assert p.links == [("/rel", "Awesome prompts"), ("https://github.com/a/b", "repo")]

    # add_link merges instead of duplicating
    lib = {"seen": {}, "links": {}, "prompts": {}}
    cs.add_link(lib, "https://github.com/a/b", "short", "", "s1", ["github"])
    cs.add_link(lib, "https://github.com/a/b", "a longer title", "d", "s2", ["github"])
    assert len(lib["links"]) == 1
    e = lib["links"]["https://github.com/a/b"]
    assert e["title"] == "a longer title"
    assert e["sources"] == ["s1", "s2"] and e["tags"] == ["github"]

    # tiktok ingest: seen, cached image bytes, pending, desc URL harvested
    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()
    urls = cs.ingest_tiktok(
        [{"url": "https://vm.tiktok.com/SHORT1/", "author": "u",
          "resolvedUrl": "https://www.tiktok.com/@u/video/1",
          "desc": "check https://github.com/x/y",
          "images": [{"url": "cdn/tos-x-p-0068/slide~tplv-tiktokx-origin.image", "b64": b64},
                     {"url": "cdn/tos-x-avt-0068/me~tplv-tiktokx-cropcenter:100:100.jpeg",
                      "b64": base64.b64encode(b"junk-avatar").decode()}],
      "video": {"url": "cdn/play",
                "b64": base64.b64encode(b"\x00\x00\x00\x18ftypmp42-fake").decode()}},
         {"url": "https://www.tiktok.com/@u/video/2", "error": "blocked"},
         {"url": "https://github.com/ride/along", "passthrough": True}], lib)
    assert urls == ["https://github.com/x/y", "https://github.com/ride/along"]
    tk = lib["links"]["https://www.tiktok.com/@u/video/1"]
    assert tk["pending"] and tk["tags"] == ["tiktok"]
    assert len(tk["images"]) == 1  # avatar url filtered out
    assert (cs.CACHE / Path(tk["images"][0]).name).read_bytes() == b"fake-jpeg-bytes"
    assert tk["video"].endswith(".mp4")
    assert (cs.CACHE / Path(tk["video"]).name).read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    # a CDN 403 body (no ftyp box) must be dropped, not cached as a video
    cs.ingest_tiktok([{"url": "https://www.tiktok.com/@u/video/3", "author": "u",
                       "images": [],
                       "video": {"url": "cdn/play",
                                 "b64": base64.b64encode(b"<html>403</html>").decode()}}], lib)
    junk = lib["links"]["https://www.tiktok.com/@u/video/3"]
    assert "video" not in junk and not junk.get("pending")
    assert "https://www.tiktok.com/@u/video/1" in lib["seen"]
    assert "https://vm.tiktok.com/SHORT1" in lib["seen"]
    assert "https://www.tiktok.com/@u/video/2" not in lib["links"]
    # sourceless findings (install backfill) must not record "" as a source
    assert cs.add_link(lib, "https://github.com/no/src", "", "", "", ["github"])["sources"] == []


    # add_extracted: repo shorthand, source chain, prompt dedup, pending cleared
    queue = cs.add_extracted(lib, [{
        "source": "https://www.tiktok.com/@u/video/1",
        "summary": "slide deck about agent prompts",
        "repos": ["foo/bar", "github.com/foo/bar", "https://github.com/foo/bar"],
        "prompts": [{"title": "P", "text": "Do the  thing.", "cat": "Coding agents"},
                    {"title": "P2", "text": "do the thing."}],  # same after normalize
        "categories": {"foo/bar": "Testing tools"},
        "installs": {"foo/bar": "pip install foobar"},
    }])
    assert queue == ["https://github.com/foo/bar"] * 3
    assert "https://github.com/github.com/foo/bar" not in lib["links"]
    assert lib["links"]["https://github.com/foo/bar"]["cat"] == "Testing tools"
    assert not lib["links"]["https://www.tiktok.com/@u/video/1"].get("pending")
    assert lib["links"]["https://github.com/foo/bar"]["sources"] == ["https://www.tiktok.com/@u/video/1"]
    assert len(lib["prompts"]) == 1
    prompt = next(iter(lib["prompts"].values()))
    assert prompt["sources"] == ["https://www.tiktok.com/@u/video/1"]
    assert prompt["cat"] == "Coding agents"  # kept through the dedup merge
    assert prompt["interpreted"] == cs.TODAY  # newest-first sort key stamped at merge
    assert lib["links"]["https://github.com/foo/bar"]["interpreted"] == cs.TODAY
    assert lib["links"]["https://www.tiktok.com/@u/video/1"]["interpreted"] == cs.TODAY
    pid = next(iter(lib["prompts"]))  # categorize accepts prompt ids as keys
    e2 = (lib["links"].get(cs.repo_url(pid)) if cs.repo_url(pid) else None) \
        or lib["prompts"].get(pid)
    assert e2 is prompt
    assert lib["links"]["https://github.com/foo/bar"]["install"] == "pip install foobar"

    # remove: gone from links, tombstoned against every future ingest path
    assert cs.remove_link(lib, "https://github.com/foo/bar")
    assert "https://github.com/foo/bar" not in lib["links"]
    cs.add_extracted(lib, [{"source": "https://www.tiktok.com/@u/video/1",
                            "repos": ["foo/bar"]}])
    cs.add_link(lib, "https://github.com/foo/bar", "t", "d", "src", ["github"])
    assert "https://github.com/foo/bar" not in lib["links"]
    assert not cs.remove_link(lib, "https://github.com/foo/bar")  # already gone

    # scan follows redirects: renamed repo merges into the final URL
    cs.fetch = lambda u: ('<title>GitHub - new/name: x</title>"stargazerCount":4200,'
                          '"isArchived":true,', "https://github.com/new/name")
    lib2 = {"seen": {}, "links": {}, "prompts": {}}
    cs.add_link(lib2, "https://github.com/new/name", "", "",
                "https://t.example/post", ["github"])
    lib2["links"]["https://github.com/new/name"]["cat"] = "Kept"
    cs.scan(lib2, ["https://github.com/old/name"])
    assert "https://github.com/old/name" not in lib2["links"]
    assert "https://github.com/old/name" in lib2["seen"]
    merged = lib2["links"]["https://github.com/new/name"]
    assert "https://t.example/post" in merged["sources"] and merged["cat"] == "Kept"
    assert merged["stars"] == 4200 and merged["archived"] == cs.TODAY

    # refresh: re-fetches lib links, updates stars, pops stale archived flag,
    # skips tiktok, tolerates fetch failures, adds no sources, no pending
    lib2["links"]["https://www.tiktok.com/@u/video/9"] = {
        "title": "@u on tiktok", "desc": "", "sources": [], "tags": ["tiktok"],
        "found": cs.TODAY}
    cs.add_link(lib2, "https://github.com/dead/gone", "t", "", "", ["github"])
    calls = []
    def fake_fetch(u):
        calls.append(u)
        if "dead/gone" in u:
            raise OSError("404")
        return ('<title>GitHub - new/name: x</title>"stargazerCount":9000,',
                "https://github.com/new/name")
    cs.fetch = fake_fetch
    cs.refresh(lib2)
    assert all("tiktok.com" not in u for u in calls)  # tiktok never re-fetched
    merged = lib2["links"]["https://github.com/new/name"]
    assert merged["stars"] == 9000 and "archived" not in merged  # un-archived
    assert not merged.get("pending") and merged["cat"] == "Kept"
    assert lib2["links"]["https://github.com/dead/gone"]["title"] == "t"  # kept on fail

    # rendered HTML embeds data, escapes script closers, keeps no placeholder
    html = cs.HTML.replace("__DATA__", json.dumps(lib).replace("</", "<\\/"))
    assert "__DATA__" not in html and "tiktok" in html and "\u2014" not in cs.HTML
    # revised intake chain: FAB add-dialog, tiktok import, interpret endpoint
    assert 'id="addFab"' in cs.HTML and 'id="tkImport"' in cs.HTML
    assert 'id="tkDir"' in cs.HTML  # configurable export folder
    assert 'data-view="pend"' in cs.HTML  # pending-items tab
    assert "Imported + interpretation finished" in cs.HTML  # import chains interpret
    assert '"/ingest"' in cs.HTML and '"/interpret"' in cs.HTML
    assert "triageBtn" not in cs.HTML and "sendBtn" not in cs.HTML  # one add button now
    # reinterpret: only entries with a cached payload get re-flagged
    lib4 = {"seen": {}, "links": {
        "https://a/": {"images": ["cache/x.jpg"]},
        "https://b/": {"video": "cache/v.mp4", "pending": False},
        "https://c/": {"title": "no cache"}}, "prompts": {}}
    assert cs.reinterpret(lib4) == 2
    assert lib4["links"]["https://a/"]["pending"] and lib4["links"]["https://b/"]["pending"]
    assert "pending" not in lib4["links"]["https://c/"]
    assert cs.reinterpret(lib4, "https://b/") == 1  # single-url mode
    assert 'id="reintAll"' in cs.HTML and "data-reint" in cs.HTML and '"/reinterpret"' in cs.HTML
    # interpretation controls live in the inbox banner, before the add dialog
    assert 'id="pendBar"' in cs.HTML
    assert cs.HTML.index('id="interpBtn"') < cs.HTML.index('id="addDlg"')
    # settings: interpret command override, blank falls back to built-in omp
    assert cs.interp_cmd({}) == cs.INTERP_CMD
    assert cs.interp_cmd({"settings": {"interpret_cmd": "  "}}) == cs.INTERP_CMD
    assert cs.interp_cmd({"settings": {"interpret_cmd": "claude -p go"}}) == "claude -p go"
    assert 'id="setDlg"' in cs.HTML and '"/settings"' in cs.HTML and "__IDEF__" in cs.HTML
    assert 'id="presets"' in cs.HTML  # harness preset chips container
    # filter dropdown covers prompt/link categories, prompts filterable by cat
    assert "p.cat === tag" in cs.HTML and "l.cat === tag" in cs.HTML
    assert "prompts.map(p => p.cat)" in cs.HTML
    # toc: persistent nav with view entries, scroll-spy, smooth anchors
    assert 'id="toc"' in cs.HTML and 'data-nav="inbox"' in cs.HTML
    assert "scroll-behavior:smooth" in cs.HTML and 'a.getAttribute("href") === "#" + cur.id' in cs.HTML
    # prompt categories: anchored h3s, toc sub-entries, spy covers h3
    assert 'class="tsub"' in cs.HTML and 'id="p-${slug(label)}"' in cs.HTML
    assert 'h2[id], h3[id]' in cs.HTML
    assert 'class="tsec"' in cs.HTML and ".toc .tsec{display:block;overflow:auto" in cs.HTML  # pinned views, scrolling cats
    assert 'class="card prompt"' in cs.HTML  # prompts share the card grid
    # search applies in the library view too (regression: isRepo early-return)
    assert 'if (view === "lib" ? !isRepo(l) : isRepo(l)) return false' in cs.HTML
    assert 'data-copy="${esc(p.id)}"' in cs.HTML and "COPY_ICON" in cs.HTML  # icon-only copy
    # favicon inlined + repo file; prompt cats have icons
    assert 'rel="icon" href="data:image/svg+xml' in cs.HTML
    assert '"Agentic coding": "' in cs.HTML and '"Game modding": "' in cs.HTML
    # install folder picker: resolves, hides dotdirs, sorts, files excluded
    droot = Path(tempfile.mkdtemp())
    (droot / "Beta").mkdir(); (droot / "alpha").mkdir(); (droot / ".git").mkdir()
    (droot / "file.txt").write_text("x")
    d = cs.list_dirs(str(droot))
    assert d["dirs"] == ["alpha", "Beta"]
    assert d["parent"] == str(Path(droot).resolve().parent)
    assert cs.list_dirs("")["path"].endswith("installs")  # blank = legacy default
    try:
        cs.list_dirs(str(droot / "nope")); assert False, "missing dir must raise"
    except OSError:
        pass
    assert "pipes a remote script" in cs.HTML  # pipe-to-shell installs get flagged
    assert 'id="instDir"' in cs.HTML and '"/dirs"' in cs.HTML and "data-d" in cs.HTML
    assert 'data-srt="new"' in cs.HTML and "localStorage.srt" in cs.HTML  # newest/az sort toggle
    # embedded JS must parse: Python escapes can silently corrupt it (seen live)
    import re as _re, shutil, subprocess, tempfile as _tf
    if shutil.which("node"):
        js = _re.findall(r"<script>(.*?)</script>", html, _re.S)[-1]
        f = Path(_tf.mkdtemp()) / "check.mjs"
        f.write_text(js.replace("<\\/", "</"), encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, "embedded JS broken:\n" + r.stderr[:500]

    # installed detection: owner/repo beats name, non-github ignored
    idx = {"dietrichgebert/ponytail": "claude plugin marketplace",
           "tdd": "skill: ~/.agents/skills/tdd"}
    lib2 = {"links": {"https://github.com/dietrichgebert/ponytail": {},
                      "https://github.com/obra/tdd": {},
                      "https://github.com/x/unrelated": {},
                      "https://example.com/tdd": {}}}
    m = cs.installed_map(lib2, idx)
    assert m["https://github.com/dietrichgebert/ponytail"] == "claude plugin marketplace"
    assert m["https://github.com/obra/tdd"].startswith("skill:")
    assert "https://github.com/x/unrelated" not in m
    assert "https://example.com/tdd" not in m
    # installed_index scans skill roots; missing plugin manifests tolerated
    root = Path(tempfile.mkdtemp())
    (root / "My-Skill").mkdir()
    cs.SKILL_ROOTS, cs.PLUGINS = [root], root / "nope"
    assert "my-skill" in cs.installed_index()
    # tree-kill: the reaper takes the agent down with its shell, live or reaped
    import os as _os, subprocess as _sp
    from clipshelf import pool as _pool
    sleeper = "ping -n 60 127.0.0.1" if _os.name == "nt" else "sleep 60"
    p = _sp.Popen(sleeper, shell=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                  start_new_session=_os.name != "nt")
    _pool._kill_tree(p)
    assert p.wait(timeout=15) is not None
    done = _sp.Popen("exit 0", shell=True)
    done.wait()
    _pool._kill_tree(done)  # already-exited process: must not raise

    print("ok")


if __name__ == "__main__":
    test()
