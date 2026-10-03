"""
Parsing and retrying the model's JSON.

A stub client stands in for the API, so nothing here reaches the network. What
is being checked is the policy: recover from a malformed response, refuse to
retry one that was truncated, and give up rather than loop.
"""
import json
import types
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import score_and_tailor as st
from score_and_tailor import parse_json_response

# The shape that killed a real resume: a missing comma between array items.
BAD = ('{"name": "Jordan Avery", "experience": [{"company": "Northwind"}\n'
       '  {"company": "Baker Media"}]}')
GOOD = '{"name": "Jordan Avery", "experience": [{"company": "Northwind"}]}'


def response(text, stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(output_tokens=8192))


class StubClient:
    """Hands back queued responses in order and counts the calls."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = 0
        self.seen_kwargs = {}
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        self.seen_kwargs = kwargs
        return self.queue.pop(0)


class ParseJsonResponse(unittest.TestCase):
    """Repairs that need no second API call."""

    def test_code_fences(self):
        self.assertEqual(parse_json_response('```json\n{"a": 1}\n```'), {"a": 1})

    def test_surrounding_prose(self):
        self.assertEqual(
            parse_json_response('Here is the resume:\n{"a": 1}\nHope that helps.'),
            {"a": 1})

    def test_line_comments(self):
        self.assertEqual(parse_json_response('{"a": 1, // the first\n "b": 2}'),
                         {"a": 1, "b": 2})

    def test_trailing_comma(self):
        self.assertEqual(parse_json_response('{"a": 1, "b": 2,}'),
                         {"a": 1, "b": 2})

    def test_genuinely_malformed_still_raises(self):
        """If this ever stops raising, the retry test below proves nothing."""
        with self.assertRaises(json.JSONDecodeError):
            parse_json_response(BAD)


class RequestJsonRetry(unittest.TestCase):
    def setUp(self):
        self.saved = st.client

    def tearDown(self):
        st.client = self.saved

    def run_with(self, *responses):
        st.client = StubClient(*responses)
        messages = [{"role": "user", "content": "draft a resume"}]
        return st.request_json(messages, 8192, "resume"), st.client, messages

    def test_malformed_then_valid_recovers(self):
        (data, _text), client, messages = self.run_with(response(BAD), response(GOOD))
        self.assertEqual(data["name"], "Jordan Avery")
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(messages), 3,
                         "the failed exchange should stay in the conversation")

    def test_valid_first_time_costs_one_call(self):
        (data, _text), client, messages = self.run_with(response(GOOD))
        self.assertEqual(data["name"], "Jordan Avery")
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(messages), 1)

    def test_malformed_twice_gives_up(self):
        """It must not loop, and the error it raises must be the parse error."""
        st.client = StubClient(response(BAD), response(BAD))
        with self.assertRaises(json.JSONDecodeError):
            st.request_json([{"role": "user", "content": "x"}], 8192, "resume")
        self.assertEqual(st.client.calls, 2)

    def test_truncation_is_not_retried(self):
        """Asking again with the same budget truncates again, so this raises
        instead of paying twice to fail twice."""
        st.client = StubClient(response(GOOD, stop_reason="max_tokens"))
        with self.assertRaises(ValueError) as caught:
            st.request_json([{"role": "user", "content": "x"}], 8192, "resume")
        self.assertEqual(st.client.calls, 1)
        self.assertIn("max_tokens", str(caught.exception))

    def test_a_schema_is_sent_as_a_constraint_when_given(self):
        """The interview's prompt has a conversational job as well as a JSON
        one, and told to do both it did the human half and dropped the JSON on
        almost every turn. The schema makes the contract the API's business."""
        st.client = StubClient(response(GOOD))
        st.request_json([{"role": "user", "content": "x"}], 8192, "interview",
                        schema={"type": "object"})
        sent = st.client.seen_kwargs
        self.assertEqual(sent["output_config"],
                         {"format": {"type": "json_schema",
                                     "schema": {"type": "object"}}})

    def test_no_schema_means_no_output_config(self):
        """Scoring and drafting only ever produce JSON, so they do not need it,
        and adding it would change three working call sites."""
        st.client = StubClient(response(GOOD))
        st.request_json([{"role": "user", "content": "x"}], 8192, "resume")
        self.assertNotIn("output_config", st.client.seen_kwargs)

    def test_the_corrective_message_quotes_the_parser(self):
        _result, _client, messages = self.run_with(response(BAD), response(GOOD))
        correction = messages[-1]["content"]
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("not valid JSON", correction)
        self.assertIn("delimiter", correction,
                      "the parser's own complaint should be handed back")


if __name__ == "__main__":
    unittest.main()
