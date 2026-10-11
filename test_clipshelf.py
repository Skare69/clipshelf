"""Self-check: python test_clipshelf.py — pure helpers and CLI lock behavior."""
import unittest
import clipshelf.lib as cs


def _automatic_migration_waits_for_exclusive_lock():
    import os
    import subprocess
    import sys
    import tempfile
    import textwrap
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="clipshelf-lock-test-") as data_dir:
        data = Path(data_dir)
        ready, release = data / "ready", data / "release"
        requested, migrated = data / "requested", data / "migrated"
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CLIPSHELF_", "DJANGO_SETTINGS_MODULE"))
        }
        env.update({
            "DJANGO_SETTINGS_MODULE": "clipshelf.project.settings",
            "CLIPSHELF_DATA_DIR": str(data),
            "CLIPSHELF_DEBUG": "1",
            "CLIPSHELF_TEST_LOCK_READY": str(ready),
            "CLIPSHELF_TEST_LOCK_RELEASE": str(release),
            "CLIPSHELF_TEST_LOCK_REQUESTED": str(requested),
            "CLIPSHELF_TEST_MIGRATED": str(migrated),
        })

        holder_code = textwrap.dedent("""\
            import os
            import time
            import django
            from pathlib import Path

            django.setup()
            from clipshelf.management.locks import data_lock
            with data_lock(exclusive=True):
                Path(os.environ["CLIPSHELF_TEST_LOCK_READY"]).touch()
                while not Path(os.environ["CLIPSHELF_TEST_LOCK_RELEASE"]).exists():
                    time.sleep(0.01)
        """)
        migration_code = textwrap.dedent("""\
            import os
            from contextlib import contextmanager
            from pathlib import Path
            import django

            django.setup()
            from django.core import management
            from clipshelf import cli
            from clipshelf.management import locks

            real_data_lock = locks.data_lock
            @contextmanager
            def tracked_data_lock(**kwargs):
                Path(os.environ["CLIPSHELF_TEST_LOCK_REQUESTED"]).touch()
                with real_data_lock(**kwargs):
                    yield

            def record_migration(*args, **kwargs):
                Path(os.environ["CLIPSHELF_TEST_MIGRATED"]).touch()

            locks.data_lock = tracked_data_lock
            management.call_command = record_migration
            management.execute_from_command_line = lambda *args, **kwargs: None
            cli.main(["worker", "--once"])
        """)

        def wait_for(path, process):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if path.exists():
                    return
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(
                        f"process exited before {path.name}: {stdout}\n{stderr}"
                    )
                time.sleep(0.01)
            raise AssertionError(f"timed out waiting for {path.name}")

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code], cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        migration = None
        try:
            wait_for(ready, holder)
            migration = subprocess.Popen(
                [sys.executable, "-c", migration_code], cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            wait_for(requested, migration)
            assert not migrated.exists(), "migration ran under the exclusive data lock"
            release.touch()
            stdout, stderr = migration.communicate(timeout=15)
            assert migration.returncode == 0, f"{stdout}\n{stderr}"
            assert migrated.exists(), "automatic migration did not run after lock release"
            stdout, stderr = holder.communicate(timeout=15)
            assert holder.returncode == 0, f"{stdout}\n{stderr}"
        finally:
            release.touch()
            if migration is not None and migration.poll() is None:
                migration.communicate(timeout=15)
            if holder.poll() is None:
                holder.communicate(timeout=15)


def _lock_check_regressions():
    import sys
    import tempfile
    from pathlib import Path
    from unittest import mock

    import scripts.lock_deps as ld

    BASE = {
        "django": "5.0.6",
        "asgiref": "3.8.1",
        "sqlparse": "0.5.0",
    }

    def lock_file(tmp, extra):
        lines = ["# test lock"]
        pins = dict(BASE)
        pins.update(extra)
        for n, v in sorted(pins.items()):
            lines.append(f"{n}=={v} \\")
            lines.append("    --hash=sha256:" + "0" * 64)
        path = tmp / "requirements.lock"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run_case(current_pins, extra):
        with tempfile.TemporaryDirectory(prefix="clipshelf-lockcheck-") as td:
            tmp = Path(td)
            req = tmp / "requirements.txt"
            req.write_text("django\n", encoding="utf-8")
            lock = lock_file(tmp, extra)
            ok_run = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(ld, "resolve", return_value=ld.Resolution(
                    pins=dict(current_pins), yanked=frozenset(),
                    gated=dict(current_pins))), \
                 mock.patch.object(ld.subprocess, "run", return_value=ok_run):
                return ld.check(sys.executable, req, lock)

    # a lock pin missing from the current resolution must fail the check;
    # typesafe-sdk's real closure pins (per requirements.lock) are absent
    typesafe_closure = {
        "typesafe-sdk": "0.7.2",
        "annotated-types": "0.8.0",
        "anyio": "4.15.1",
        "httpcore2": "2.13.1",
        "httpx2": "2.13.1",
        "pydantic": "2.13.5",
        "pydantic-core": "2.46.5",
        "typing-inspection": "0.4.4",
    }
    assert run_case(dict(BASE), typesafe_closure) != 0
    # the only tolerated absence is Django's win32-only tzdata marker dep
    assert run_case(dict(BASE), {"tzdata": "2024.1"}) == 0
    # Django itself stays present in both scenarios (it is in BASE currents)


def _lock_gate_regressions():
    import contextlib
    import io
    import sys
    import tempfile
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from unittest import mock

    import scripts.lock_deps as ld

    with tempfile.TemporaryDirectory(prefix="clipshelf-lockgate-") as td:
        tmp = Path(td)
        req = tmp / "requirements.txt"
        req.write_text("django\npydantic\n", encoding="utf-8")

        # existing behavior: a newer lock pin (a security bump) stays,
        # compared numerically; an older pin and a pin the requirements no
        # longer resolve do not
        gated = {"django": "5.2.9", "asgiref": "3.12.1", "pydantic": "2.13.5"}
        calls = []
        held_seen = []

        def fake_dry_run(python, req, tmp, extra):
            calls.append(list(extra))
            if extra and extra[0] == "-c":
                held_seen.append(Path(extra[1]).read_text())
                held = dict(l.split("==") for l in held_seen[-1].split())
                return {n: held.get(n, "99.0") for n in gated}, set()  # an unheld name gets its newest
            return dict(gated), set()

        with mock.patch.object(ld, "dry_run", side_effect=fake_dry_run):
            keep = {"django": "5.2.18", "asgiref": "3.11.0", "gone": "1.0"}
            res = ld.resolve("python", req, tmp, keep)
            assert res.pins == {"django": "5.2.18", "asgiref": "3.12.1", "pydantic": "2.13.5"}
            # the held pass always runs now; the keep-only pin is held too
            assert len(calls) == 2 and calls[1][0] == "-c"
            assert "gone==1.0" in held_seen[0]
            # nothing newer in the lock: the gated pass alone decides
            calls.clear(); held_seen.clear()
            assert ld.resolve("python", req, tmp, {"django": "5.2.9"}).pins == gated
            assert len(calls) == 2 and calls[1][0] == "-c"
            # --fresh unholds that pin only: the second pass takes its newest release
            calls.clear(); held_seen.clear()
            assert ld.resolve("python", req, tmp, fresh=["Pydantic"]).pins == {
                "django": "5.2.9", "asgiref": "3.12.1", "pydantic": "99.0"}
            assert "pydantic" not in held_seen[0]
            # a fresh name is never held, even when its keep pin is newer
            calls.clear(); held_seen.clear()
            assert ld.resolve("python", req, tmp, {"django": "5.2.18"},
                              fresh=["django"]).pins["django"] == "99.0"
            assert "django" not in held_seen[0]

        # 1. cutoff exactness: the gated pass is exactly MIN_RELEASE_AGE back
        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

        seen = []
        with mock.patch.object(ld, "datetime", Fixed), \
                mock.patch.object(ld, "dry_run",
                                  side_effect=lambda p, r, t, e: (seen.append(list(e)) or ({"x": "1.0"}, set()))):
            ld.resolve("python", req, tmp)
        assert seen[0] == ["--uploaded-prior-to", "2026-10-04T12:00:00Z"]
        assert ld.MIN_RELEASE_AGE == timedelta(days=7)

        # 2. a yanked keep pin is unheld and re-resolved, never shipped
        def yanked_fake(python, req, tmp, extra):
            if extra and extra[0] == "-c":
                held = dict(l.split("==") for l in Path(extra[1]).read_text().split())
                if held.get("foo") == "2.0":
                    return {"foo": "2.0"}, {"foo"}
                return {"foo": "1.0"}, set()
            return {"foo": "1.0"}, set()

        with mock.patch.object(ld, "dry_run", side_effect=yanked_fake):
            res = ld.resolve("python", req, tmp, {"foo": "2.0"})
        assert res.pins == {"foo": "1.0"} and not res.yanked

        lock_lines = ["# test lock", "foo==2.0 \\", "    --hash=sha256:" + "0" * 64]
        lock = tmp / "requirements.lock"
        lock.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
        ok_run = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(ld, "resolve", return_value=ld.Resolution(
                pins={"foo": "1.0"}, yanked={"foo"}, gated={"foo": "1.0"})), \
                mock.patch.object(ld.subprocess, "run", return_value=ok_run):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ld.check("python", req, lock)
        assert rc == 1
        assert "changed: foo lock=2.0 current=1.0" in out.getvalue()
        assert "yanked: foo==2.0" in out.getvalue()

        # 3. a young security floor must not break the gate: the age-gated
        # pass resolves a floor-free copy; the floor is never held against itself
        assert ld._floor_free("Django>=5.2.19,<5.3") == "Django <5.3"
        assert ld._floor_free("yt-dlp[curl-cffi]>=2026.8.19") == "yt-dlp[curl-cffi]"
        assert ld._floor_free("# comment") == "# comment"
        assert ld._floor_free("foo>=1; python_version < '3.10'") == "foo ; python_version < '3.10'"
        req2 = tmp / "req-floor.txt"
        req2.write_text("Django>=5.2.19,<5.3\n", encoding="utf-8")

        def floor_fake(python, req, tmp, extra):
            if extra and extra[0] == "--uploaded-prior-to":
                text = Path(req).read_text()
                if ">=" in text or "<5.3" not in text:
                    sys.exit("No matching distribution")
                return {"django": "5.2.18"}, set()
            if extra:  # -c held pass
                if "django" in Path(extra[1]).read_text():
                    sys.exit("floor held against itself")
            return {"django": "5.2.19"}, set()

        with mock.patch.object(ld, "dry_run", side_effect=floor_fake):
            res = ld.resolve("python", req2, tmp, {"django": "5.2.17"}, fresh=["django"])
        assert res.pins == {"django": "5.2.19"}

        # 4. --fresh that backtracks exits naming the held pins that block it
        bt = {"pydantic": "2.13.5", "pydantic-core": "2.46.5"}

        def back_fake(python, req, tmp, extra):
            if extra == []:
                return {"pydantic": "2.14.1", "pydantic-core": "2.50.1"}, set()
            if extra and extra[0] == "-c":
                held = dict(l.split("==") for l in Path(extra[1]).read_text().split())
                assert "pydantic" not in held, held
                return {"pydantic": "2.13.5",
                        "pydantic-core": held.get("pydantic-core", "9.0")}, set()
            return dict(bt), set()

        with mock.patch.object(ld, "dry_run", side_effect=back_fake):
            try:
                ld.resolve("python", req, tmp,
                           keep={"pydantic": "2.13.5", "pydantic-core": "2.46.5"},
                           fresh=["pydantic"])
                raise AssertionError("backtracked --fresh must exit")
            except SystemExit as e:
                msg = str(e)
        assert "pydantic" in msg and "pydantic-core" in msg

        # 5. keep-only pins are held too, so they cannot float past the age gate
        def keep_fake(python, req, tmp, extra):
            if extra and extra[0] == "-c":
                held = dict(l.split("==") for l in Path(extra[1]).read_text().split())
                assert held.get("newdep") == "1.5", held
                return {"parent": "2.0", "newdep": held.get("newdep", "9.0")}, set()
            return {"parent": "1.0"}, set()

        with mock.patch.object(ld, "dry_run", side_effect=keep_fake):
            res = ld.resolve("python", req, tmp, {"parent": "2.0", "newdep": "1.5"})
        assert res.pins["newdep"] == "1.5"

        # 6. make exits on additions that resolved only because a held/fresh
        # pin pulled them in, without the age gate
        with mock.patch.object(ld, "resolve", return_value=ld.Resolution(
                pins={"parent": "2.0", "newdep": "9.0"}, yanked=set(),
                gated={"parent": "1.0"})), \
                mock.patch.object(ld, "collect_hashes", return_value={}):
            try:
                ld.make("python", req, tmp / "out.lock", None)
                raise AssertionError("ungated addition must exit")
            except SystemExit as e:
                assert "newdep" in str(e)

        # 7. the lock's win32-only tzdata pin is carried into the resolution
        def tz_fake(python, req, tmp, extra):
            return {"django": "5.2.17"}, set()

        with mock.patch.object(ld, "dry_run", side_effect=tz_fake):
            res = ld.resolve("python", req, tmp, {"django": "5.2.17", "tzdata": "2025.2"})
        assert res.pins["tzdata"] == "2025.2"


def test():
    # norm: canonicalize + dedup
    assert cs.norm("https://GitHub.com/a/b/?utm_source=x#frag") == cs.norm("https://github.com/a/b")
    assert cs.norm("https://github.com/Foo/Bar") == cs.norm("https://github.com/foo/bar")
    assert cs.norm("https://github.com/Foo/Bar/blob/Main/SomeFile.py") == \
        "https://github.com/foo/bar/blob/Main/SomeFile.py"
    # deep GitHub ref/file path case is preserved; owner/repo still folds
    assert cs.norm("https://github.com/foo/bar/blob/Main/ReadMe.md") == "https://github.com/foo/bar/blob/Main/ReadMe.md"
    assert cs.norm("https://github.com/foo/bar/blob/Main/F.py") != cs.norm("https://github.com/foo/bar/blob/main/f.py")
    assert cs.norm("https://Example.com/CaseMatters") != cs.norm("https://example.com/casematters")
    assert cs.repo_url("Foo/Bar") == "https://github.com/foo/bar"
    assert cs.repo_url("github.com/Foo/Bar") == "https://github.com/foo/bar"
    assert cs.repo_url("https://github.com/Foo/Bar") == "https://github.com/foo/bar"
    assert cs.norm("ftp://x") is None and cs.norm("not a url") is None
    # hostile authorities rejected: credentials, malformed/oversized ports, bad brackets
    assert cs.norm("http://user:pass@example.com/x") is None
    assert cs.norm("http://user@example.com/x") is None
    assert cs.norm("http://example.com:port/x") is None
    assert cs.norm("http://example.com:99999999999/x") is None
    assert cs.norm("http://[::1/x") is None
    assert cs.norm("http:///no-host") is None
    # IPv6 stays intact and dedups; explicit ports are preserved
    assert cs.norm("https://[2001:DB8::1]:8443/a") == "https://[2001:db8::1]:8443/a"
    assert cs.norm("http://[::ffff:127.0.0.1]/x") == "http://[::ffff:127.0.0.1]/x"
    assert cs.norm("http://[::1]:80/a") == "http://[::1]:80/a"
    assert cs.norm("  https://example.com/x ,") == "https://example.com/x"
    assert cs.norm("https://example.com/a?si=abc&x=1") == "https://example.com/a?x=1"

    # tagging + terminal hosts
    assert cs.tag_for("https://github.com/foo/bar") == "github"
    assert cs.tag_for("https://gist.github.com/x/1") == "github"
    assert cs.tag_for("https://huggingface.co/x") == "huggingface"
    assert cs.tag_for("https://example.com/x", "Awesome Prompt list") == "prompt"
    assert cs.tag_for("https://example.com/boring") is None
    assert cs.tag_for("https://example.com/x", "Beginner Tutorial") == "tutorial"
    # github nav chrome / non-repo shapes are rejected
    for noise in ("https://github.com", "https://github.com/anthropics",
                  "https://github.com/features/copilot", "https://github.com/login",
                  "https://github.com/a/b/blob/main/x.ipynb", "https://docs.github.com"):
        assert cs.tag_for(noise) is None, noise
    # every GitHub nav namespace stays non-repo even with a second path segment
    for w in ("customer-stories", "readme", "events", "explore", "settings",
              "notifications", "login", "join", "signup", "contact", "new",
              "why-github", "edu", "open-source", "orgs", "enterprise", "security"):
        assert cs.tag_for(f"https://github.com/{w}/x") is None, w
    assert cs.is_terminal("https://gist.github.com/x/1")
    assert cs.is_terminal("https://github.com/a/b") and not cs.is_terminal("https://example.com")
    for word in ("features", "pricing", "topics", "collections", "trending", "marketplace",
                 "sponsors", "about", "site", "orgs", "enterprise", "security",
                 "customer-stories", "readme", "events", "explore", "settings",
                 "notifications", "login", "join", "signup", "contact", "new",
                 "why-github", "edu", "open-source"):
        assert not cs.github_repo(f"https://github.com/{word}/x"), word
    assert cs.github_repo("https://github.com/owner/x")

    assert cs.is_terminal("https://www.arxiv.org/abs/1")
    assert cs.tag_for("https://arxiv.org/abs/1") == "arxiv"
    assert cs.tag_for("https://colab.research.google.com/drive/1") == "colab"
    assert cs.tag_for("https://www.huggingface.co/x") == "huggingface"
    # every keyword matches via url path and via anchor text
    for k in ("prompt", "guide", "tutorial", "awesome", "cheatsheet", "workflow", "docs"):
        assert cs.tag_for(f"https://example.com/{k}") == k, k
        assert cs.tag_for("https://example.com/x", f"Best {k.upper()} ever") == k, k
    assert cs.tag_for("https://example.com/zqx") is None
    # http scheme is allowed, not just https
    assert cs.norm("http://example.com/x") == "http://example.com/x"

    # shortener detection
    for short in ("https://share.google/abc", "https://goo.gl/x", "https://g.co/x",
                  "https://bit.ly/x", "https://tinyurl.com/x", "https://vt.tiktok.com/x",
                  "https://redd.it/x",
                  "https://vm.tiktok.com/ZS8x",
                  "https://vt.tiktok.com/ZS9y",
                  "https://www.reddit.com/r/x/s/TOKEN", "https://www.tiktok.com/t/ZTx"):
        assert cs.is_short(short), short
    for full in ("https://www.tiktok.com/@u/video/1", "https://reddit.com/r/x/comments/1/y",
                 "https://github.com/a/b"):
        assert not cs.is_short(full), full

    # parser: title, meta desc, links with anchor text
    p = cs.PageParser()
    p.feed('<title>T</title><meta name="description" content="D">'
           '<a href="/rel">Awesome  prompts</a><a href="https://github.com/a/b">repo</a>')
    assert p.title == "T" and p.desc == "D"
    assert p.links == [("/rel", "Awesome prompts"), ("https://github.com/a/b", "repo")]
    # character references are decoded everywhere (convert_charrefs=True)
    p = cs.PageParser()
    p.feed('<title>A &amp; B</title>'
           '<meta name="description" content="O&#39;Reilly &quot;q&quot;">'
           '<a href="/x">C &amp; D</a><p>E &#39;F&#39; &quot;G&quot;</p>')
    p.close()
    assert p.title == "A & B", p.title
    assert p.desc == "O'Reilly \"q\"", p.desc
    assert p.links == [("/x", "C & D")], p.links
    assert p.text.endswith("E 'F' \"G\""), p.text  # title and anchor text are body text too
    p = cs.PageParser()
    p.feed('<meta property="og:description" content="OG">')
    assert p.desc == "OG"

    # readable body text: non-rendered containers excluded, block boundaries split
    p = cs.PageParser()
    p.feed('<head><title>T</title><style>.x{color:red}</style></head>'
           '<script>evil()</script><template>tpl</template><noscript>nope</noscript>'
           '<svg><text>hidden</text></svg>'
           '<p>First para</p><div>second<b>bold</b></div>')
    p.close()
    assert p.title == "T"
    assert p.text == "First para secondbold", p.text  # inline tags add no space
    blocks = ("p", "div", "li", "ul", "ol", "tr", "table", "section", "article",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure",
              "header", "footer", "main", "nav", "aside", "dl", "dt", "dd")
    p = cs.PageParser()
    p.feed("".join(f"<{tag}>w{i}</{tag}>" for i, tag in enumerate(blocks)) + "a<br>b")
    p.close()
    assert p.text == " ".join(f"w{i}" for i in range(len(blocks))) + " a b", p.text

    # readable text is bounded
    p = cs.PageParser()
    p.feed("<p>" + "word " * (cs.TEXT_LIMIT // 4))
    p.close()
    assert len(p.text) == 100_000  # TEXT_LIMIT: the documented model-input bound

    # meta description: first one wins; og:description is a fallback
    p = cs.PageParser()
    p.feed('<meta name="description" content="first">'
           '<meta name="description" content="second">')
    assert p.desc == "first"
    p = cs.PageParser()
    p.feed('<meta property="og:description" content="og">')
    assert p.desc == "og"
    # anchor without href ignored; anchor text collected across data chunks/inline tags
    p = cs.PageParser()
    p.feed('<a>no href</a><a href="/x">A<b>B</b> <!-- c -->C</a>')
    assert p.links == [("/x", "AB C")], p.links
    # title closes: body text must not leak into .title
    p = cs.PageParser()
    p.feed('<title>T</title><p>Body</p>')
    p.close()
    assert p.title == "T" and "Body" in p.text
    # <br> splits the line
    p = cs.PageParser()
    p.feed('a<br>b')
    p.close()
    assert p.text == "a b", p.text  # newline from <br> collapses to a space
    # nested skip tags: skip counter returns to 0 after the outer container
    p = cs.PageParser()
    p.feed('<script>a<style>b</style>c</script><p>vis</p>')
    p.close()
    assert p.text == "vis", p.text
    # every skip tag's content is excluded
    for tag in ("script", "style", "template", "noscript", "svg", "head"):
        p = cs.PageParser()
        p.feed(f'<{tag}>hidden</{tag}><p>shown</p>')
        p.close()
        assert p.text == "shown", (tag, p.text)
    # block-level closes start a new line
    for tag in ("p", "div", "li", "ul", "ol", "tr", "table", "section", "article",
                "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure",
                "header", "footer", "main", "nav", "aside", "dl", "dt", "dd"):
        p = cs.PageParser()
        p.feed(f'<{tag}>a</{tag}><{tag}>b</{tag}>')
        p.close()
        assert p.text == "a b", (tag, p.text)

    _automatic_migration_waits_for_exclusive_lock()
    _lock_check_regressions()
    _lock_gate_regressions()
    print("ok")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.FunctionTestCase(test)])


if __name__ == "__main__":
    test()
