from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest

from deploy_stats.report import report
from deploy_stats.store import _open


class TrendTests(unittest.TestCase):
    def test_24h_rates_are_computed_from_eligible_requests_and_include_partial_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'series.sqlite3'
            now = datetime(2026, 1, 3, 12, 2, 3, tzinfo=timezone(timedelta(hours=8))).timestamp()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('fixture', now - 2400, now, now, 0))
            statement = '''INSERT INTO requests(run_id,finished,client_ip,elapsed,http_status,state,
                input_tokens,output_tokens,cached_tokens,session_id,first_visible) VALUES(?,?,?,?,?,?,?,?,?,?,?)'''
            for elapsed, prompt, output, cached, first in ((3, 100, 11, 90, 1),
                                                            (5, 10, 3, 0, 2),
                                                            (1, 0, 1, 0, 0),
                                                            (2, 20, 2, 20, None)):
                db.execute(statement, ('fixture', now - 900, '192.0.2.4', elapsed, 200, 'known',
                                       prompt, output, cached, None, first))
            db.execute(statement, ('fixture', now - 900, '192.0.2.4', 1, 500, 'failed',
                                   None, None, None, None, None))
            db.execute(statement, ('fixture', now - 100, '192.0.2.4', 3, 200, 'known', 20, 5, 0, None, 1))
            db.commit()
            db.close()
            points = report(path, 'today', now=now)['series_24h']
            bucket_start = int((now - 900) // 600) * 600
            bucket = next(point for point in points if point['start'] == bucket_start)
            self.assertAlmostEqual(bucket['mean_request_hit_rate'], 1.9 / 3)
            self.assertEqual(bucket['request_hit_samples'], 3)
            self.assertAlmostEqual(bucket['prefill_estimate_tps'], 20 / 3)
            self.assertEqual((bucket['prefill_samples'], bucket['prefill_tokens'], bucket['prefill_seconds']), (2, 20, 3))
            self.assertAlmostEqual(bucket['decode_estimate_tps'], 12 / 5)
            self.assertEqual((bucket['decode_samples'], bucket['decode_tokens'], bucket['decode_seconds']), (2, 12, 5))
            empty = next(point for point in points if point['start'] == bucket_start - 600)
            self.assertTrue(empty['complete'])
            self.assertIsNone(empty['mean_request_hit_rate'])
            self.assertIsNone(empty['prefill_estimate_tps'])
            self.assertIsNone(empty['decode_estimate_tps'])
            partial = next(point for point in points if point['start'] <= now - 100 < point['end'])
            self.assertLess(partial['end'] - partial['start'], 600)
            self.assertAlmostEqual(sum(point['request_hit_sum'] for point in points) /
                                   sum(point['request_hit_samples'] for point in points), 1.9 / 4)

    def test_historical_overload_detail_is_not_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'overload.sqlite3'
            now = time.time()
            db = _open(path)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('old', now - 20, now - 10, now - 10, 1))
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', ('new', now - 10, now, now, 3))
            db.execute('INSERT INTO stats_meta VALUES (?,?)', ('overload:new', '3'))
            db.commit()
            db.close()
            totals = report(path, 'total', now=now + .01)['totals']
            self.assertEqual((totals['dropped'], totals['overload_rejected'],
                              totals['overload_tracked_runs'], totals['overload_untracked_runs']), (4, 3, 1, 1))


if __name__ == '__main__':
    unittest.main()
