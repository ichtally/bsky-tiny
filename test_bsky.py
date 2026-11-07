"""Offline tests for rich-text facets. Run: python3 -m unittest test_bsky"""
import unittest
from bsky import parse_facets


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


if __name__ == "__main__":
    unittest.main()
