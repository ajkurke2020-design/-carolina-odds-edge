import unittest

from app_auth import passphrase_matches


class PassphraseMatchTests(unittest.TestCase):
    def test_ascii_passphrase_matches_only_when_identical(self):
        self.assertTrue(passphrase_matches("correct horse battery", "correct horse battery"))
        self.assertFalse(passphrase_matches("correct horse battery!", "correct horse battery"))
        self.assertFalse(passphrase_matches("", "correct horse battery"))

    def test_non_ascii_passphrase_does_not_raise_type_error(self):
        expected = "pässwörd-with-ünïcode-✓"
        self.assertTrue(passphrase_matches(expected, expected))
        self.assertFalse(passphrase_matches("pässwörd-with-ünïcode-x", expected))

    def test_non_ascii_entry_against_ascii_secret_is_rejected_cleanly(self):
        self.assertFalse(passphrase_matches("café-passphrase-123", "cafe-passphrase-123"))


if __name__ == "__main__":
    unittest.main()
