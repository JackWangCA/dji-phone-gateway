import importlib.util
import pathlib
import sys
import unittest

PATH = pathlib.Path(__file__).parents[1] / "src" / "sms_bridge.py"
spec = importlib.util.spec_from_file_location("sms_bridge", PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class ParsingTests(unittest.TestCase):
    def test_bot_suffix_and_quoted_message(self):
        self.assertEqual(bridge.parse_command('/sms@mybot +1555 "hello world"'), ("/sms", ["+1555", "hello world"]))

    def test_phone_validation(self):
        self.assertTrue(bridge.safe_phone("+1-555-123-4567"))
        self.assertFalse(bridge.safe_phone("+1; reboot"))

    def test_sms_flattens_cli_control_characters(self):
        self.assertEqual(bridge.safe_sms("hello\ncore stop now\x00"), "hello core stop now")

    def test_unauthorized_chat_is_silent(self):
        class Telegram:
            def send(self, *_):
                raise AssertionError("must not reply to unauthorized users")
        cfg = bridge.Config("token", 42, "user", "password")
        bridge.handle(cfg, Telegram(), 7, "/help")


if __name__ == "__main__":
    unittest.main()
