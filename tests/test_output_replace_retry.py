"""Bounded Windows replace retries preserve OS failures and rollback behavior."""
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from pko.output import publisher


def windows_error(number):
    error = OSError(f"Windows error {number}")
    error.winerror = number
    return error


class OutputReplaceRetryTests(unittest.TestCase):
    def test_transient_native_access_conflicts_retry_then_replace(self):
        for number in (5, 32, 33):
            with self.subTest(winerror=number):
                source, target = Mock(spec=Path), Mock(spec=Path)
                source.replace.side_effect = [windows_error(number), windows_error(number), target]
                with patch.object(publisher.sys, "platform", "win32"), \
                        patch.object(publisher.time, "sleep") as sleep:
                    publisher._replace(source, target)
                self.assertEqual(source.replace.call_args_list, [call(target)] * 3)
                self.assertEqual(sleep.call_args_list, [call(0.05), call(0.1)])

    def test_permanent_access_conflict_raises_original_error_after_bounded_attempts(self):
        source, target = Mock(spec=Path), Mock(spec=Path)
        error = windows_error(5)
        source.replace.side_effect = error
        with patch.object(publisher.sys, "platform", "win32"), \
                patch.object(publisher.time, "sleep") as sleep, \
                self.assertRaises(OSError) as caught:
            publisher._replace(source, target)
        self.assertIs(caught.exception, error)
        self.assertEqual(source.replace.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertLessEqual(sum(entry.args[0] for entry in sleep.call_args_list), 1.0)

    def test_unrelated_windows_errors_and_non_windows_errors_are_not_retried(self):
        for platform, error in (("win32", windows_error(2)), ("win32", windows_error(112)),
                                ("win32", PermissionError("ordinary denial")),
                                ("linux", windows_error(5)), ("darwin", windows_error(32))):
            with self.subTest(platform=platform, error=str(error)):
                source, target = Mock(spec=Path), Mock(spec=Path)
                source.replace.side_effect = error
                with patch.object(publisher.sys, "platform", platform), \
                        patch.object(publisher.time, "sleep") as sleep, \
                        self.assertRaises(OSError) as caught:
                    publisher._replace(source, target)
                self.assertIs(caught.exception, error)
                source.replace.assert_called_once_with(target)
                sleep.assert_not_called()

    def test_successful_replace_does_not_wait(self):
        source, target = Mock(spec=Path), Mock(spec=Path)
        with patch.object(publisher.time, "sleep") as sleep:
            publisher._replace(source, target)
        source.replace.assert_called_once_with(target)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
