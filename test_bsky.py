"""Offline tests for rich-text facets and link cards. Run: python3 -m unittest test_bsky"""
import unittest
from bsky import parse_card, parse_facets


def spans(text, facets):
    b = text.encode("utf-8")
    return [(b[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode(), f["features"][0]["$type"].split("#")[1])
            for f in facets]


class Facets(unittest.TestCase):
    def test_link_at_start(self):
        t = "https://example.com/a?b=1 is the page"
        self.assertEqual(spans(t, parse_facets(t)), [("https://example.com/a?b=1", "link")])

    def test_trailing_punctuation_not_in_link(self):
        t = "see https://example.com/x."
        self.assertEqual(spans(t, parse_facets(t)), [("https://example.com/x", "link")])

    def test_multibyte_before_link(self):
        t = "café ☕ 🙂 → https://example.org/ok"
        f = parse_facets(t)
        self.assertEqual(spans(t, f), [("https://example.org/ok", "link")])
        self.assertNotEqual(f[0]["index"]["byteStart"], t.index("https"))  # bytes, not code points

    def test_mentions_resolved(self):
        t = "hi @alice.bsky.social and @nobody.invalid"
        f = parse_facets(t, lambda h: "did:plc:alice" if h == "alice.bsky.social" else None)
        self.assertEqual(spans(t, f), [("@alice.bsky.social", "mention")])
        self.assertEqual(f[0]["features"][0]["did"], "did:plc:alice")

    def test_no_mentions_without_resolver(self):
        self.assertEqual(parse_facets("hi @alice.bsky.social"), [])

    def test_email_is_not_mention(self):
        self.assertEqual(parse_facets("mail bob@example.com", lambda h: "did:plc:x"), [])

    def test_tags(self):
        t = "#python is fun, #日本語 too! not#this #123"
        f = parse_facets(t)
        self.assertEqual(spans(t, f), [("#python", "tag"), ("#日本語", "tag")])
        self.assertEqual(f[1]["features"][0]["tag"], "日本語")

    def test_url_fragment_not_tag(self):
        t = "read https://example.com/page#section now"
        self.assertEqual([k for _, k in spans(t, parse_facets(t))], ["link"])

    def test_sorted_and_mixed(self):
        t = "#a @b.test https://c.test/d"
        f = parse_facets(t, lambda h: "did:plc:b")
        self.assertEqual([k for _, k in spans(t, f)], ["tag", "mention", "link"])

    # The official app's quirks, reproduced on purpose (checked against @atproto/api v0.30.3).
    def test_bare_domains_need_a_real_tld(self):
        t = "see example.com, readme.md and notes.txt"
        f = parse_facets(t)
        self.assertEqual(spans(t, f), [("example.com", "link"), ("readme.md", "link")])
        self.assertEqual(f[0]["features"][0]["uri"], "https://example.com")

    def test_tld_check_is_case_sensitive(self):
        self.assertEqual(parse_facets("Example.COM and HTTPS://a.com"), [])

    def test_cashtags(self):
        t = "$aapl up, ($TSLA) but not $toolong"
        f = parse_facets(t)
        self.assertEqual(spans(t, f), [("$aapl", "tag"), ("$TSLA", "tag")])
        self.assertEqual(f[0]["features"][0]["tag"], "$AAPL")

    def test_fullwidth_hash_and_unicode_punctuation(self):
        t = "＃タグ and #tag… and #!!!"
        f = parse_facets(t)
        self.assertEqual(spans(t, f), [("＃タグ", "tag"), ("#tag", "tag")])


class Cards(unittest.TestCase):
    def test_open_graph_tags(self):
        page = """<html><head><title>Fallback</title>
          <META property="og:title" content="Caf&eacute; &amp; tools" />
          <meta property="og:description" content="  Two
            lines  ">
          <meta property="og:image" content="/img/card.png"></head><body>x</body></html>"""
        self.assertEqual(parse_card(page, "https://example.com/a/page.html"), {
            "title": "Café & tools", "description": "Two lines", "image": "https://example.com/img/card.png"})

    def test_fallbacks(self):
        page = '<title>\n  Plain title </title><meta name="description" content="d"><meta name="twitter:image" ' \
               'content="https://cdn.test/i.jpg">'
        self.assertEqual(parse_card(page, "https://x.test/"),
                         {"title": "Plain title", "description": "d", "image": "https://cdn.test/i.jpg"})

    def test_first_tag_wins_and_empty_page(self):
        page = '<meta property="og:title" content="one"><meta property="og:title" content="two">'
        self.assertEqual(parse_card(page, "https://x.test/")["title"], "one")
        self.assertEqual(parse_card("", "https://x.test/"), {"title": "", "description": "", "image": ""})

    def test_card_survives_a_failing_image(self):
        import io, contextlib, urllib.error, bsky
        page = b'<meta property="og:title" content="T"><meta property="og:image" content="/i.png">'

        def fake_fetch(url, limit):
            if url.endswith("/i.png"):
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            return page, "text/html", "utf-8", url
        real, bsky._fetch = bsky._fetch, fake_fetch
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                emb = bsky.link_card("https://x.test/p", token="unused")
        finally:
            bsky._fetch = real
        self.assertEqual(emb, {"$type": "app.bsky.embed.external",
                               "external": {"uri": "https://x.test/p", "title": "T", "description": ""}})
        self.assertIn("card image skipped", err.getvalue())


class SelfLabels(unittest.TestCase):
    PROFILE = {"$type": "app.bsky.actor.profile", "displayName": "Me", "description": "bio",
               "avatar": {"$type": "blob", "ref": {"$link": "bafk"}, "mimeType": "image/png", "size": 1}}

    def test_add_keeps_everything_else(self):
        from bsky import set_self_label
        new = set_self_label(self.PROFILE, "bot")
        self.assertEqual(new["labels"], {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "bot"}]})
        self.assertEqual({k: v for k, v in new.items() if k != "labels"}, self.PROFILE)
        self.assertNotIn("labels", self.PROFILE)  # input not modified

    def test_add_twice_and_other_labels(self):
        from bsky import set_self_label
        rec = dict(self.PROFILE, labels={"$type": "com.atproto.label.defs#selfLabels",
                                         "values": [{"val": "!no-unauthenticated"}]})
        once = set_self_label(rec, "bot")
        self.assertEqual(set_self_label(once, "bot"), once)
        self.assertEqual([v["val"] for v in once["labels"]["values"]], ["!no-unauthenticated", "bot"])
        self.assertEqual(rec["labels"]["values"], [{"val": "!no-unauthenticated"}])

    def test_remove(self):
        from bsky import set_self_label
        on = set_self_label(self.PROFILE, "bot")
        self.assertEqual(set_self_label(on, "bot", on=False), self.PROFILE)  # empty list dropped
        self.assertEqual(set_self_label(self.PROFILE, "bot", on=False), self.PROFILE)


if __name__ == "__main__":
    unittest.main()
