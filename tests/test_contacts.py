"""The address book: collecting, Google vCards, merging and searching.

    python3 -m unittest tests.test_contacts
"""
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from postroom import carddav, contacts
from postroom.contacts import GOOGLE, Contact
from postroom.models import Message
from postroom.store import Store


def msg(from_name="", from_addr="", to="", cc="", ts=1.0):
    return Message(account_id="a", folder="INBOX", uid=1, subject="s", from_name=from_name,
                   from_addr=from_addr, to=to, cc=cc, date_ts=ts)


VCARDS = """BEGIN:VCARD\r
VERSION:3.0\r
FN:Ann Lee\r
N:Lee;Ann;;;\r
EMAIL;TYPE=INTERNET;TYPE=HOME:ann@example.com\r
item1.EMAIL;TYPE=INTERNET:Ann.Work@Example.org\r
END:VCARD\r
BEGIN:VCARD\r
VERSION:3.0\r
N:Stone;Bob;;;\r
EMAIL:bob@example.com\r
NOTE:a long note that is\r
 folded onto a second line\r
END:VCARD\r
BEGIN:VCARD\r
VERSION:3.0\r
FN:No Email Person\r
TEL:555\r
END:VCARD\r
"""


class HarvestTest(unittest.TestCase):
    def test_senders_outside_sent(self):
        found = contacts.harvest([msg("Ann Lee", "Ann@Example.com"), msg("", "me@gmail.com"),
                                  msg("Shop", "no-reply@shop.com"), msg("X", "not-an-address")],
                                 own={"me@gmail.com"}, sent_folder=False)
        self.assertEqual([(c.email, c.name, c.score) for c in found],
                         [("ann@example.com", "Ann Lee", contacts.W_HEARD_FROM)])

    def test_recipients_in_sent_count_more_and_keep_info_addresses(self):
        found = contacts.harvest([msg(to='"Bob Stone" <bob@example.com>, info@smallshop.com',
                                      cc="ann@example.com")],
                                 own=set(), sent_folder=True)
        by = {c.email: c for c in found}
        self.assertEqual(set(by), {"bob@example.com", "info@smallshop.com", "ann@example.com"})
        self.assertEqual(by["bob@example.com"].name, "Bob Stone")
        self.assertEqual(by["bob@example.com"].score, contacts.W_WROTE_TO)

    def test_name_same_as_address_is_dropped(self):
        found = contacts.harvest([msg("ann@example.com", "ann@example.com")], set(), False)
        self.assertEqual(found[0].name, "")

    def test_address_and_spoken(self):
        self.assertEqual(Contact("ann@example.com", "Lee, Ann").address, '"Lee, Ann" <ann@example.com>')
        self.assertEqual(Contact("ann@example.com").address, "ann@example.com")
        self.assertEqual(Contact("ann@example.com", "Ann Lee").spoken, "Ann Lee, ann@example.com")


class VCardTest(unittest.TestCase):
    def test_parse(self):
        found = contacts.parse_vcards(VCARDS)
        self.assertEqual([(c.email, c.name, c.source) for c in found], [
            ("ann@example.com", "Ann Lee", GOOGLE),
            ("ann.work@example.org", "Ann Lee", GOOGLE),
            ("bob@example.com", "Bob Stone", GOOGLE),  # name from N when FN is missing
        ])


class TokenTest(unittest.TestCase):
    def test_current_token(self):
        text = "Ann Lee <ann@example.com>, bo, carol@x.org"
        self.assertEqual(contacts.current_token(text, 29)[2], "bo")
        start, end, tok = contacts.current_token(text, len(text))
        self.assertEqual((tok, text[start:end]), ("carol@x.org", "carol@x.org"))
        self.assertEqual(contacts.current_token("an", 2), (0, 2, "an"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "cache.db")

    def test_merge_and_rank(self):
        s = self.store
        s.add_contacts([Contact("ann@example.com", "", score=1, last_ts=5)])
        s.add_contacts([Contact("ann@example.com", "Ann L", score=1, last_ts=9)])
        s.add_contacts([Contact("andy@example.com", "Andy", score=3)])
        names = [(c.email, c.name, c.score) for c in s.search_contacts("an")]
        self.assertEqual(names, [("andy@example.com", "Andy", 3), ("ann@example.com", "Ann L", 2)])
        # A Google contact wins the name and jumps ahead, once.
        g = Contact("ann@example.com", "Ann Lee", GOOGLE, contacts.W_GOOGLE)
        s.add_contacts([g])
        s.add_contacts([g])
        top = s.search_contacts("an")[0]
        self.assertEqual((top.email, top.name, top.source, top.score),
                         ("ann@example.com", "Ann Lee", GOOGLE, 2 + contacts.W_GOOGLE))

    def test_search_by_any_name_word_and_escaping(self):
        s = self.store
        s.add_contacts([Contact("x1@example.com", "Robyn Smith"), Contact("a_b@example.com", "")])
        self.assertEqual([c.email for c in s.search_contacts("smi")], ["x1@example.com"])
        self.assertEqual([c.email for c in s.search_contacts("a_")], ["a_b@example.com"])
        self.assertEqual(s.search_contacts("a%"), [])
        self.assertEqual(s.search_contacts(""), [])


class FakeCardDav(http.server.BaseHTTPRequestHandler):
    """Two cards over two multiget batches; insists on the bearer token."""
    seen = []

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PROPFIND(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers["Authorization"] != "Bearer good":
            return self._reply(401)
        FakeCardDav.seen.append(("PROPFIND", self.headers["Depth"]))
        self._reply(207, b"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
<d:response><d:href>/book/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>
</d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/book/1.vcf</d:href><d:propstat><d:prop><d:getetag>"1"</d:getetag>
<d:resourcetype/></d:prop></d:propstat></d:response>
<d:response><d:href>/book/2.vcf</d:href><d:propstat><d:prop><d:getetag>"2"</d:getetag>
<d:resourcetype/></d:prop></d:propstat></d:response></d:multistatus>""")

    def do_REPORT(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        FakeCardDav.seen.append(("REPORT", body.count("<d:href>")))
        cards = {"/book/1.vcf": VCARDS.split("END:VCARD")[0] + "END:VCARD",
                 "/book/2.vcf": "BEGIN:VCARD\nFN:Bob &amp; Co\nEMAIL:bob@example.com\nEND:VCARD"}
        parts = "".join(
            f'<d:response><d:href>{h}</d:href><d:propstat><d:prop><c:address-data>{v}'
            f"</c:address-data></d:prop></d:propstat></d:response>"
            for h, v in cards.items() if f"<d:href>{h}</d:href>" in body)
        self._reply(207, ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                          f'xmlns:c="urn:ietf:params:xml:ns:carddav">{parts}</d:multistatus>').encode())

    def log_message(self, *a):
        pass


class CardDavTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeCardDav)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/book/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fetch_in_batches(self):
        FakeCardDav.seen.clear()
        orig = carddav.BATCH
        carddav.BATCH = 1
        try:
            found = carddav.fetch(self.url, "good")
        finally:
            carddav.BATCH = orig
        self.assertEqual(sorted((c.email, c.name) for c in found), [
            ("ann.work@example.org", "Ann Lee"), ("ann@example.com", "Ann Lee"),
            ("bob@example.com", "Bob & Co")])
        self.assertEqual(FakeCardDav.seen, [("PROPFIND", "1"), ("REPORT", 1), ("REPORT", 1)])

    def test_refused_token_is_explained(self):
        with self.assertRaisesRegex(carddav.CardDavError, "Sign in again"):
            carddav.fetch(self.url, "expired")


if __name__ == "__main__":
    unittest.main()
