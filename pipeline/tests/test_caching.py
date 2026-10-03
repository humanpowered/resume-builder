"""
Prompt caching depends on one property: the cached block must be byte-identical
across requests. Nothing reports a miss -- a changed prefix just quietly costs
full price, so the property is worth asserting rather than assuming.

Also checks the two blocks still concatenate to the prompt that was sent before
the split, since the point was to cache the profile, not to reword anything.

A stub client stands in for the API; no calls go out.
"""
import json
import types
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first
from helpers import profile

import score_and_tailor as st
from score_and_tailor import cached_messages, profile_preamble


def posting(title="Director, Marketing Analytics", company="Acme"):
    return {"title": title, "company": company, "location": "Remote",
            "description_html": "<p>Lead the analytics team.</p>",
            "url": "http://example.com/1", "score": 8, "reasoning": "fits"}


class CachedMessages(unittest.TestCase):
    def test_the_stable_block_carries_the_marker_and_the_other_does_not(self):
        blocks = cached_messages("stable", "volatile")[0]["content"]
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", blocks[1])

    def test_one_breakpoint_only(self):
        """Four per request is the ceiling; spending more than one here would
        cache the posting too, which never repeats."""
        blocks = cached_messages("stable", "volatile")[0]["content"]
        marked = [b for b in blocks if "cache_control" in b]
        self.assertEqual(len(marked), 1)

    def test_the_blocks_concatenate_to_the_original_prompt(self):
        stable = profile_preamble("Do the thing.", profile())
        rest = "\nJOB POSTING:\nTitle: Director"
        joined = stable + rest
        self.assertIn("Do the thing.\n\nCANDIDATE PROFILE:\n", joined)
        self.assertIn("}\n\nJOB POSTING:\nTitle: Director", joined,
                      "the blank line between profile and posting must survive")
        self.assertNotIn("\n\n\n", joined, "no doubled blank line at the seam")


class TheCachedPrefixIsStable(unittest.TestCase):
    """The property the saving rests on: same profile, different postings, same
    first block."""

    def setUp(self):
        self.saved = st.client
        self.sent = []

        def create(**kwargs):
            self.sent.append(kwargs["messages"])
            raise RuntimeError("stop; the request is what we wanted")

        st.client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create))

    def tearDown(self):
        st.client = self.saved

    def first_blocks_for(self, fn, postings):
        firsts = []
        for p in postings:
            self.sent.clear()
            try:
                fn(p, profile())
            except RuntimeError:
                pass
            firsts.append(self.sent[0][0]["content"][0])
        return firsts

    def test_scoring_sends_the_same_cached_block_for_any_posting(self):
        a, b = self.first_blocks_for(
            st.score_posting,
            [posting(), posting("VP, Data Science", "Globex")])
        self.assertEqual(a["text"], b["text"])
        self.assertIn("cache_control", a)

    def test_resume_sends_the_same_cached_block_for_any_posting(self):
        a, b = self.first_blocks_for(
            st.tailor_resume,
            [posting(), posting("VP, Data Science", "Globex")])
        self.assertEqual(a["text"], b["text"])

    def test_cover_letter_sends_the_same_cached_block_for_any_posting(self):
        a, b = self.first_blocks_for(
            st.draft_cover_letter,
            [posting(), posting("VP, Data Science", "Globex")])
        self.assertEqual(a["text"], b["text"])

    def test_the_posting_is_not_in_the_cached_block(self):
        """If the posting leaked into the cached half, every request would write
        its own entry and nothing would ever be read."""
        for fn in (st.score_posting, st.tailor_resume, st.draft_cover_letter):
            with self.subTest(fn=fn.__name__):
                block = self.first_blocks_for(fn, [posting()])[0]
                self.assertNotIn("Acme", block["text"])
                self.assertNotIn("JOB POSTING", block["text"])

    def test_the_cached_block_is_the_preamble_and_nothing_more(self):
        """There is deliberately no assertion here about length. Below the
        model's minimum cacheable prefix -- 1024 tokens on this family -- the
        marker is silently ignored, and the fixture profile in helpers.py is a
        fraction of that, as a thin real profile would be: the marker costs
        nothing and simply buys nothing. What can be checked is that the block
        holds the preamble and the whole profile, which is all there is to
        cache."""
        block = self.first_blocks_for(st.score_posting, [posting()])[0]
        self.assertTrue(block["text"].endswith("}\n"))
        self.assertIn("CANDIDATE PROFILE:", block["text"])
        self.assertIn(profile()["work_history"][-1]["company"], block["text"],
                      "the whole profile belongs in the cached half")


class SpendSummary(unittest.TestCase):
    """The run has to be able to say whether the cache was read. A cache that
    quietly stops being read costs ten times more per call and raises nothing."""

    def setUp(self):
        self.saved = dict(st._SPEND)

    def tearDown(self):
        st._SPEND.update(self.saved)

    def tally(self, **kw):
        st._SPEND.update(fresh=0, written=0, read=0, out=0, calls=0)
        st._tally(types.SimpleNamespace(
            input_tokens=kw.get("fresh", 0),
            cache_creation_input_tokens=kw.get("written", 0),
            cache_read_input_tokens=kw.get("read", 0),
            output_tokens=kw.get("out", 0)))
        return st.spend_summary()

    def test_nothing_ran(self):
        st._SPEND.update(fresh=0, written=0, read=0, out=0, calls=0)
        self.assertEqual(st.spend_summary(), "")

    def test_a_cache_read_is_reported_as_a_saving(self):
        line = self.tally(fresh=1964, read=7380, out=260)
        self.assertIn("saved", line)
        self.assertNotIn("[warn]", line)

    def test_a_write_with_no_reads_warns(self):
        """One changed byte in the preamble produces exactly this: entries
        written on every call and never read."""
        line = self.tally(fresh=2090, written=7380, out=435)
        self.assertIn("[warn]", line)
        self.assertIn("varying", line)

    def test_usage_without_cache_fields_does_not_crash(self):
        st._SPEND.update(fresh=0, written=0, read=0, out=0, calls=0)
        st._tally(types.SimpleNamespace(input_tokens=100, output_tokens=10))
        self.assertIn("model call", st.spend_summary())

    def test_an_unknown_model_still_reports(self):
        saved = st.MODEL
        st.MODEL = "some-model-released-later"
        try:
            self.assertIn("model call", self.tally(fresh=100, out=10))
        finally:
            st.MODEL = saved


class ProfileOrderIsDeterministic(unittest.TestCase):
    def test_the_same_profile_renders_the_same_bytes(self):
        """json.dumps follows insertion order, so a profile rebuilt in a
        different order would render differently and miss the cache."""
        p = profile()
        self.assertEqual(profile_preamble("x", p), profile_preamble("x", p))
        reordered = {k: p[k] for k in reversed(list(p))}
        self.assertNotEqual(
            profile_preamble("x", p), profile_preamble("x", reordered),
            "if this ever passes, key order stopped mattering and the "
            "determinism note in profile_preamble can go")
        self.assertEqual(json.loads(profile_preamble("x", p).split(":\n", 1)[1]),
                         json.loads(profile_preamble("x", reordered).split(":\n", 1)[1]),
                         "same data either way -- only the bytes differ")


if __name__ == "__main__":
    unittest.main()
