# test_recalibrate.py - fresher rates: the Stop hook measures them again after a day (not 14), at once when
# rates.json was written in an older format, and only one session at a time.
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from fixtures import IsolatedTest
import autoscore
import calibrate


class Recalibrate(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.rates = self.data / "rates.json"
        self.runs = 0

        def fake_main() -> None:                         # stands in for a real (one-minute) calibration
            self.runs += 1
            self.rates.write_text(json.dumps({"format": calibrate.RATES_FORMAT, "models": {}}), encoding="utf-8")
        patcher = mock.patch.object(calibrate, "main", fake_main)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rates(self, age_days: float, fmt: int | None = None) -> None:
        body = {"models": {}} if fmt is None else {"format": fmt, "models": {}}
        self.rates.write_text(json.dumps(body), encoding="utf-8")
        old = time.time() - age_days * 86400
        os.utime(self.rates, (old, old))

    def test_two_day_old_rates_are_measured_again(self):
        self.write_rates(2, calibrate.RATES_FORMAT)
        self.assertTrue(autoscore.recalibrate_if_stale(time.time()))
        self.assertEqual(self.runs, 1)

    def test_fresh_rates_in_the_current_format_are_kept(self):
        self.write_rates(0.1, getattr(calibrate, "RATES_FORMAT", 2))
        self.assertFalse(autoscore.recalibrate_if_stale(time.time()))
        self.assertEqual(self.runs, 0)

    def test_fresh_rates_from_an_older_version_are_measured_again(self):
        self.write_rates(0.1)                            # no "format": written before this version
        self.assertTrue(autoscore.recalibrate_if_stale(time.time()))

    def test_one_calibration_at_a_time(self):
        self.write_rates(3, calibrate.RATES_FORMAT)
        lock = self.data / "calibrating.lock"
        lock.write_text("1234", encoding="utf-8")        # another session is calibrating right now
        self.assertFalse(autoscore.recalibrate_if_stale(time.time()))
        old = time.time() - 2 * autoscore.CALIBRATE_LOCK_SECONDS
        os.utime(lock, (old, old))                       # ... or it died long ago
        self.assertTrue(autoscore.recalibrate_if_stale(time.time()))
        self.assertFalse(lock.exists())                  # the lock is removed afterwards

    def test_calibrate_writes_the_format_first(self):
        rates, _, _ = calibrate.measure_rates(1)
        self.assertEqual(next(iter(rates)), "format")
        self.assertEqual(rates["format"], calibrate.RATES_FORMAT)


if __name__ == "__main__":
    unittest.main()
