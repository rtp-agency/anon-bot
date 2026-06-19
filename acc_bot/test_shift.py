"""Unit tests for shift-window and reconciliation rendering logic.

Run: python -m unittest test_shift.py -v
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Force the module to use a throwaway DB so init_db doesn't touch prod data
os.environ["ACC_DB_PATH"] = ":memory:"

import bot  # noqa: E402

MSK = bot.MOSCOW_TZ


def msk(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


class TestWorkingDay(unittest.TestCase):
    def _check(self, shift, ref, expected):
        with patch.object(bot, "get_bot_shift", return_value=shift):
            got = bot.current_working_day("TOKEN", ref_dt=ref)
        self.assertEqual(got, expected, f"shift={shift} ref={ref} -> {got}, expected {expected}")

    def test_normal_day_shift_anywhere_in_day(self):
        # 0–23 shift = full day, working day == calendar day in MSK
        for hour in (0, 6, 12, 18, 23):
            self._check({"start": 0, "end": 23}, msk(2026, 6, 10, hour), "2026-06-10")

    def test_overnight_shift_after_midnight_belongs_to_yesterday(self):
        # Argentina 13–11: if now is 09:00, working day is yesterday
        self._check({"start": 13, "end": 11}, msk(2026, 6, 10, 9), "2026-06-09")

    def test_overnight_shift_before_close_still_yesterday(self):
        # Same shift at 10:59 — still yesterday's working day
        self._check({"start": 13, "end": 11}, msk(2026, 6, 10, 10, 59), "2026-06-09")

    def test_overnight_shift_after_close_is_today(self):
        # 13:00 — new shift started today
        self._check({"start": 13, "end": 11}, msk(2026, 6, 10, 13), "2026-06-10")

    def test_overnight_16_5(self):
        # Ecuador-style shift: 16-5
        self._check({"start": 16, "end": 5}, msk(2026, 6, 10, 4), "2026-06-09")
        self._check({"start": 16, "end": 5}, msk(2026, 6, 10, 5), "2026-06-09")
        self._check({"start": 16, "end": 5}, msk(2026, 6, 10, 6), "2026-06-10")
        self._check({"start": 16, "end": 5}, msk(2026, 6, 10, 23), "2026-06-10")


class TestShiftWindow(unittest.TestCase):
    def test_normal_day_window(self):
        with patch.object(bot, "get_bot_shift", return_value={"start": 0, "end": 23}):
            start_ts, end_ts = bot.shift_window_for_date("TOKEN", "2026-06-10")
        self.assertEqual(datetime.fromtimestamp(start_ts, MSK), msk(2026, 6, 10, 0, 0))
        # end is 23:59:59.999
        end_dt = datetime.fromtimestamp(end_ts, MSK)
        self.assertEqual((end_dt.hour, end_dt.minute), (23, 59))

    def test_overnight_window_spans_two_calendar_days(self):
        with patch.object(bot, "get_bot_shift", return_value={"start": 16, "end": 5}):
            start_ts, end_ts = bot.shift_window_for_date("TOKEN", "2026-06-10")
        # shift "2026-06-10" started 16:00 on June 10, ends 05:59 on June 11
        self.assertEqual(datetime.fromtimestamp(start_ts, MSK), msk(2026, 6, 10, 16, 0))
        end_dt = datetime.fromtimestamp(end_ts, MSK)
        self.assertEqual((end_dt.year, end_dt.month, end_dt.day), (2026, 6, 11))
        self.assertEqual(end_dt.hour, 5)

    def test_minute_granularity_start(self):
        shift = {"start": 16, "end": 6, "start_min": 30, "end_min": 30}
        with patch.object(bot, "get_bot_shift", return_value=shift):
            start_ts, end_ts = bot.shift_window_for_date("TOKEN", "2026-06-10")
        start_dt = datetime.fromtimestamp(start_ts, MSK)
        end_dt = datetime.fromtimestamp(end_ts, MSK)
        self.assertEqual((start_dt.hour, start_dt.minute), (16, 30))
        self.assertEqual((end_dt.year, end_dt.month, end_dt.day), (2026, 6, 11))
        self.assertEqual((end_dt.hour, end_dt.minute, end_dt.second), (6, 30, 59))

    def test_minute_granularity_working_day_boundary(self):
        # Overnight 16:30 -> 06:30. At 06:25 still belongs to yesterday's shift.
        shift = {"start": 16, "end": 6, "start_min": 30, "end_min": 30}
        with patch.object(bot, "get_bot_shift", return_value=shift):
            self.assertEqual(
                bot.current_working_day("TOK", ref_dt=msk(2026, 6, 11, 6, 25)),
                "2026-06-10")
            # At 06:35 the new shift hasn't started yet (15:50 either) — between
            # shifts, working day is today.
            self.assertEqual(
                bot.current_working_day("TOK", ref_dt=msk(2026, 6, 11, 6, 35)),
                "2026-06-11")


class TestPreviousShiftDate(unittest.TestCase):
    def test_inside_today_returns_yesterday(self):
        # 0-23 shift, mid-day: previous closed shift is yesterday
        with patch.object(bot, "get_bot_shift", return_value={"start": 0, "end": 23}):
            prev = bot.previous_shift_date("TOKEN", ref_dt=msk(2026, 6, 10, 12))
        self.assertEqual(prev, "2026-06-09")

    def test_overnight_before_close_returns_day_before_yesterday(self):
        # Shift 16-5. At 04:00 on the 10th, today's working day is the 9th,
        # so previous closed shift is the 8th.
        with patch.object(bot, "get_bot_shift", return_value={"start": 16, "end": 5}):
            prev = bot.previous_shift_date("TOKEN", ref_dt=msk(2026, 6, 10, 4))
        self.assertEqual(prev, "2026-06-08")

    def test_overnight_after_close_returns_yesterday(self):
        # Same shift after close (e.g. 06:00) — today's working day is the 10th,
        # previous closed shift is the 9th.
        with patch.object(bot, "get_bot_shift", return_value={"start": 16, "end": 5}):
            prev = bot.previous_shift_date("TOKEN", ref_dt=msk(2026, 6, 10, 6))
        self.assertEqual(prev, "2026-06-09")


class TestNumberFormatting(unittest.TestCase):
    def test_truncate_to_tenths(self):
        self.assertEqual(bot._truncate_to_tenths(15.78), 15.7)
        self.assertEqual(bot._truncate_to_tenths(15.79), 15.7)
        self.assertEqual(bot._truncate_to_tenths(15.0), 15.0)
        self.assertEqual(bot._truncate_to_tenths(-3.99), -3.9)

    def test_fmt_amount_integer(self):
        self.assertEqual(bot.fmt_amount(1234567), "1.234.567")
        self.assertEqual(bot.fmt_amount(0), "0")

    def test_fmt_amount_decimal(self):
        self.assertEqual(bot.fmt_amount(1234.78), "1.234,7")
        self.assertEqual(bot.fmt_amount(0.99), "0,9")

    def test_fmt_usd(self):
        self.assertEqual(bot.fmt_usd(100), "100")
        self.assertEqual(bot.fmt_usd(123.456), "123.4")

    def test_fmt_rate_preserves_two_decimals(self):
        # 10.08 must stay 10,08 — no truncation
        self.assertEqual(bot.fmt_rate(10.08), "10,08")
        self.assertEqual(bot.fmt_rate(10.0), "10")
        self.assertEqual(bot.fmt_rate(1495), "1.495")
        self.assertEqual(bot.fmt_rate(3690.5), "3.690,50")
        self.assertEqual(bot.fmt_rate(3690.55), "3.690,55")
        self.assertEqual(bot.fmt_rate(0.99), "0,99")


class TestPrivateChatGuard(unittest.TestCase):
    def test_private_chat_passes(self):
        from types import SimpleNamespace as NS
        chat = NS(type="private")
        msg = NS(chat=chat)
        update = NS(effective_message=msg, callback_query=None)
        self.assertTrue(bot._is_private_chat(update))

    def test_group_chat_blocked(self):
        from types import SimpleNamespace as NS
        chat = NS(type="group")
        msg = NS(chat=chat)
        update = NS(effective_message=msg, callback_query=None)
        self.assertFalse(bot._is_private_chat(update))

    def test_supergroup_blocked(self):
        from types import SimpleNamespace as NS
        chat = NS(type="supergroup")
        msg = NS(chat=chat)
        update = NS(effective_message=msg, callback_query=None)
        self.assertFalse(bot._is_private_chat(update))


if __name__ == "__main__":
    unittest.main(verbosity=2)
