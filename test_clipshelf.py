"""Self-check: python test_clipshelf.py — pure URL/tag/parser helpers only."""
import clipshelf.lib as cs


def test():
    # norm: canonicalize + dedup
    assert cs.norm("https://GitHub.com/a/b/?utm_source=x#frag") == cs.norm("https://github.com/a/b")
    assert cs.norm("https://github.com/Foo/Bar") == cs.norm("https://github.com/foo/bar")
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

    # parser: title, meta desc, links with anchor text
    p = cs.PageParser()
    p.feed('<title>T</title><meta name="description" content="D">'
           '<a href="/rel">Awesome  prompts</a><a href="https://github.com/a/b">repo</a>')
    assert p.title == "T" and p.desc == "D"
    assert p.links == [("/rel", "Awesome prompts"), ("https://github.com/a/b", "repo")]

    # readable body text: non-rendered containers excluded, block boundaries split
    p = cs.PageParser()
    p.feed('<head><title>T</title><style>.x{color:red}</style></head>'
           '<script>evil()</script><template>tpl</template><noscript>nope</noscript>'
           '<svg><text>hidden</text></svg>'
           '<p>First para</p><div>second<b>bold</b></div>')
    p.close()
    assert p.title == "T"
    assert p.text == "First para secondbold", p.text  # inline tags add no space

    # readable text is bounded
    p = cs.PageParser()
    p.feed("<p>" + "word " * (cs.TEXT_LIMIT // 4))
    p.close()
    assert len(p.text) <= cs.TEXT_LIMIT

    print("ok")


if __name__ == "__main__":
    test()
