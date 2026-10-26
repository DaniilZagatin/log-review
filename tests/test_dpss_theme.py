"""The standalone passport carries its DPSS typography offline."""
import base64
import re
import unittest

from pko.render.dpss_theme import embedded_fonts


class DpssThemeTests(unittest.TestCase):
    def test_embedded_woff2_for_every_used_weight(self):
        html = embedded_fonts()
        self.assertEqual(html.count('@font-face'), 3)
        for weight in (400, 600, 700):
            self.assertIn(f'font-weight:{weight};', html)
        payloads = re.findall(r'base64,([^\)]+)\)', html)
        self.assertEqual(len(payloads), 3)
        for payload in payloads:
            self.assertEqual(base64.b64decode(payload, validate=True)[:4], b'wOF2')
        self.assertNotIn('https://', html)
        self.assertNotIn('file://', html)


if __name__ == '__main__':
    unittest.main()
