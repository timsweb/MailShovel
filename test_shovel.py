"""Tests for shovel.fetch_and_forward using a fake IMAP server and Gmail service."""
import base64
import os
import tempfile
import unittest
from unittest import mock

import httplib2
from googleapiclient.errors import HttpError

import shovel

UIDVALIDITY = 100


def raw(n, extra_headers=""):
    return (f"From: a@example.com\r\n{extra_headers}Subject: msg {n}\r\n"
            f"Message-ID: <{n}@example.com>\r\n\r\nbody {n}\r\n").encode()


def http_error(status, reason):
    resp = httplib2.Response({"status": status})
    content = ('{"error": {"message": "%s"}}' % reason).encode()
    return HttpError(resp, content)


class FakeIMAP:
    """Just enough of IMAPClient for fetch_and_forward."""

    def __init__(self, messages, fail_add_flags_for=()):
        self.messages = messages  # uid -> raw bytes
        self.flags = {uid: set() for uid in messages}
        self.fail_add_flags_for = set(fail_add_flags_for)
        self.uidvalidity = UIDVALIDITY

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def select_folder(self, name):
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        if criteria == ["UNSEEN"]:
            return [u for u in self.messages if shovel.imapclient.SEEN not in self.flags[u]]
        if criteria[:2] == ["HEADER", "Message-ID"]:
            return [u for u, m in self.messages.items() if criteria[2].encode() in m]
        raise AssertionError(criteria)

    def fetch(self, uids, items):
        assert items == ["BODY.PEEK[]"], "must not use a fetch that sets \\Seen"
        return {u: {b"BODY[]": self.messages[u]} for u in uids}

    def add_flags(self, uids, flags):
        for u in uids:
            if u in self.fail_add_flags_for:
                self.fail_add_flags_for.discard(u)
                raise shovel.imapclient.exceptions.IMAPClientError("store failed: Mailbox was deleted under us")
            self.flags[u].update(flags)


class FakeGmail:
    """Records imported messages; import_ can be made to fail for chosen bodies."""

    def __init__(self, failures=None):
        self.imported = []
        self.failures = failures or {}  # substring of raw message -> HttpError

    def users(self):
        return self

    def messages(self):
        return self

    def import_(self, userId, body):
        msg = base64.urlsafe_b64decode(body["raw"])
        request = mock.Mock()

        def execute():
            for needle, err in self.failures.items():
                if needle in msg:
                    raise err
            self.imported.append(msg)
            return {"id": str(len(self.imported))}

        request.execute = execute
        return request


ACCOUNT = {"imap_host": "imap.example.com", "imap_port": 993,
           "username": "user@example.com", "password": "x"}


class FetchAndForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(shovel, "PENDING_SEEN_PATH", os.path.join(self.tmp.name, "pending.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, imap, gmail, pending):
        with mock.patch.object(shovel.imapclient, "IMAPClient", imap):
            shovel.fetch_and_forward(ACCOUNT, gmail, pending)

    def test_imports_and_marks_seen(self):
        imap, gmail = FakeIMAP({1: raw(1), 2: raw(2)}), FakeGmail()
        self.run_once(imap, gmail, {})
        self.assertEqual(len(gmail.imported), 2)
        self.assertTrue(all(shovel.imapclient.SEEN in f for f in imap.flags.values()))

    def test_permanent_rejection_is_flagged_and_rest_continue(self):
        imap = FakeIMAP({1: raw(1, "From: b@example.com\r\n"), 2: raw(2)})
        gmail = FakeGmail({b"msg 1": http_error(400, "Expected a single 'From' header")})
        self.run_once(imap, gmail, {})
        self.assertEqual(imap.flags[1], {shovel.imapclient.SEEN, shovel.imapclient.FLAGGED})
        self.assertEqual(len(gmail.imported), 1)
        self.assertIn(shovel.imapclient.SEEN, imap.flags[2])

    def test_label_400_is_not_treated_as_bad_message(self):
        imap = FakeIMAP({1: raw(1)})
        gmail = FakeGmail({b"msg 1": http_error(400, "Invalid label: Label_123")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertEqual(imap.flags[1], set())

    def test_too_many_rejections_stops(self):
        msgs = {u: raw(u) for u in range(1, 6)}
        imap = FakeIMAP(msgs)
        gmail = FakeGmail({b"msg": http_error(400, "Invalid argument")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertEqual(sum(1 for f in imap.flags.values() if f), shovel.MAX_REJECTIONS_PER_RUN)

    def test_transient_failure_leaves_remaining_unseen(self):
        imap = FakeIMAP({1: raw(1), 2: raw(2), 3: raw(3)})
        gmail = FakeGmail({b"msg 2": http_error(503, "Backend Error")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertIn(shovel.imapclient.SEEN, imap.flags[1])
        self.assertEqual(imap.flags[2], set())
        self.assertEqual(imap.flags[3], set())

    def test_flag_failure_after_import_is_not_reimported(self):
        imap, gmail = FakeIMAP({1: raw(1), 2: raw(2)}, fail_add_flags_for=[1]), FakeGmail()
        pending = {}
        with self.assertRaises(shovel.imapclient.exceptions.IMAPClientError):
            self.run_once(imap, gmail, pending)
        self.assertEqual(len(pending[ACCOUNT["username"]]), 1)

        # Next attempt: message 1 is still unseen on the server but already in Gmail.
        self.run_once(imap, gmail, shovel.load_pending_seen())
        self.assertEqual([m.count(b"msg 1") for m in gmail.imported], [1, 0])
        self.assertTrue(all(shovel.imapclient.SEEN in f for f in imap.flags.values()))
        self.assertEqual(shovel.load_pending_seen()[ACCOUNT["username"]], [])

    def test_pending_matched_by_message_id_when_uidvalidity_changes(self):
        imap, gmail = FakeIMAP({7: raw(1)}), FakeGmail()
        pending = {ACCOUNT["username"]: [{"uidvalidity": 1, "uid": 1, "message_id": "<1@example.com>"}]}
        self.run_once(imap, gmail, pending)
        self.assertEqual(gmail.imported, [])
        self.assertIn(shovel.imapclient.SEEN, imap.flags[7])


class WithRetryTest(unittest.TestCase):
    def test_retries_once(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("Temporary failure in name resolution")
            return "ok"

        with mock.patch.object(shovel.time, "sleep"):
            self.assertEqual(shovel.with_retry("x", flaky), "ok")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
