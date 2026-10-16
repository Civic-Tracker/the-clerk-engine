"""python3 -m unittest discover -s tests   (stdlib only, no network)"""
import io, os, sys, unittest, urllib.error, urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import net


class Resp:
    status = 200
    headers = {"content-type": "application/json"}
    def read(self):
        return b'{"ok":1}'


def err(code, ra=None):
    h = {"Retry-After": ra} if ra else {}
    return urllib.error.HTTPError("https://x/y", code, "e", h, io.BytesIO(b"x"))


class NetTest(unittest.TestCase):
    def req(self):
        return urllib.request.Request("https://example.invalid/a")

    def test_retries_429_and_5xx_then_ok(self):
        seq = [err(429, "1"), err(503), TimeoutError("slow"), Resp()]
        sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=seq):
            s, _, b = net.request(self.req(), tries=5, sleep=sleeps.append)
        self.assertEqual((s, b), (200, b'{"ok":1}'))
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(sleeps[0], 1.0)  # Retry-After honored

    def test_404_returned_without_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=[err(404)]) as m:
            s, _, _ = net.request(self.req(), sleep=lambda x: None)
        self.assertEqual(s, 404); self.assertEqual(m.call_count, 1)

    def test_no_retry_hook(self):  # NetFile PDF 500 = no image on file
        with mock.patch("urllib.request.urlopen", side_effect=[err(500)]) as m:
            s, _, _ = net.request(self.req(), no_retry=lambda c: c == 500, sleep=lambda x: None)
        self.assertEqual(s, 500); self.assertEqual(m.call_count, 1)

    def test_gives_up(self):
        with mock.patch("urllib.request.urlopen", side_effect=[err(502)] * 3):
            with self.assertRaises(RuntimeError):
                net.request(self.req(), tries=3, sleep=lambda x: None)

    def test_backoff_bounded(self):
        for a in range(10):
            self.assertLessEqual(net.backoff(a), 60.0)


if __name__ == "__main__":
    unittest.main()
