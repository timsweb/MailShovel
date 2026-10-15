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
        self.imported_labels = []
        self.import_kwargs = []
        self.failures = failures or {}  # substring of raw message -> HttpError
        self.label_ids = {}

    def users(self):
        return self

    def labels(self):
        gmail = self
        labels = mock.Mock()
        labels.list.return_value.execute.side_effect = lambda: {
            "labels": [{"name": n, "id": i} for n, i in gmail.label_ids.items()]}

        def create(userId, body):
            gmail.label_ids[body["name"]] = f"Label_{len(gmail.label_ids) + 1}"
            request = mock.Mock()
            request.execute.return_value = {"id": gmail.label_ids[body["name"]]}
            return request

        labels.create.side_effect = create
        return labels

    def messages(self):
        return self

    def import_(self, userId, body, **kwargs):
        msg = base64.urlsafe_b64decode(body["raw"])
        request = mock.Mock()

        def execute():
            for needle, err in self.failures.items():
                if needle in msg:
                    raise err
            self.imported.append(msg)
            self.imported_labels.append(body["labelIds"])
            self.import_kwargs.append(kwargs)
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
        patcher = mock.patch.object(shovel, "LABEL_CACHE_PATH", os.path.join(self.tmp.name, "labels.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(shovel, "label_cache", {})
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
        gmail = FakeGmail({b"body 1": http_error(400, "Expected a single 'From' header")})
        self.run_once(imap, gmail, {})
        self.assertEqual(imap.flags[1], {shovel.imapclient.SEEN, shovel.imapclient.FLAGGED})
        self.assertIn(shovel.imapclient.SEEN, imap.flags[2])
        # The notice for message 1, then message 2 itself.
        self.assertEqual(len(gmail.imported), 2)
        self.assertIn(b"body 2", gmail.imported[1])

    def test_rejection_leaves_notice_in_gmail(self):
        imap = FakeIMAP({1: raw(1, "From: b@example.com\r\n")})
        gmail = FakeGmail({b"body 1": http_error(400, "Expected a single 'From' header")})
        account = dict(ACCOUNT, apply_labels=["acct"])
        with mock.patch.object(shovel.imapclient, "IMAPClient", imap):
            shovel.fetch_and_forward(account, gmail, {})

        [notice], [labels] = gmail.imported, gmail.imported_labels
        self.assertNotIn(b"body 1", notice)
        text = notice.decode()
        for expected in ("msg 1", "a@example.com", "Expected a single 'From' header", ACCOUNT["username"]):
            self.assertIn(expected, text)
        self.assertNotIn("INBOX", labels)
        self.assertIn("UNREAD", labels)
        self.assertIn(gmail.label_ids[shovel.REJECTED_LABEL], labels)
        self.assertIn(gmail.label_ids["acct"], labels)
        # Gmail's spam filter would otherwise bin notices about spam.
        self.assertEqual(gmail.import_kwargs, [{"neverMarkSpam": True}])
        # Not from the account itself, which Gmail may know as a send-as address and file under Sent.
        self.assertIn("From: MailShovel <mailshovel@example.com>", text)

    def test_original_stays_unseen_if_notice_fails(self):
        imap = FakeIMAP({1: raw(1)})
        gmail = FakeGmail({b"body 1": http_error(400, "Invalid attachment"),
                           b"couldn't import": http_error(503, "Backend Error")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertEqual(imap.flags[1], set())

    def test_encoded_subject_is_decoded_in_notice(self):
        imap = FakeIMAP({1: raw(1).replace(b"Subject: msg 1", b"Subject: =?UTF-8?B?U2hhcmUgWW91ciBGZWVkYmFjaw==?=")})
        gmail = FakeGmail({b"body 1": http_error(400, "Invalid attachment")})
        self.run_once(imap, gmail, {})
        self.assertIn("Share Your Feedback", gmail.imported[0].decode())

    def test_label_400_is_not_treated_as_bad_message(self):
        imap = FakeIMAP({1: raw(1)})
        gmail = FakeGmail({b"body 1": http_error(400, "Invalid label: Label_123")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertEqual(imap.flags[1], set())

    def test_too_many_rejections_stops(self):
        msgs = {u: raw(u) for u in range(1, 6)}
        imap = FakeIMAP(msgs)
        gmail = FakeGmail({b"body": http_error(400, "Invalid argument")})
        with self.assertRaises(HttpError):
            self.run_once(imap, gmail, {})
        self.assertEqual(sum(1 for f in imap.flags.values() if f), shovel.MAX_REJECTIONS_PER_RUN)
        self.assertEqual(len(gmail.imported), shovel.MAX_REJECTIONS_PER_RUN)

    def test_transient_failure_leaves_remaining_unseen(self):
        imap = FakeIMAP({1: raw(1), 2: raw(2), 3: raw(3)})
        gmail = FakeGmail({b"body 2": http_error(503, "Backend Error")})
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
