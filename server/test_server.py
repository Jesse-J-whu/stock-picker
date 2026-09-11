import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import pandas as pd
import fast_qfq
from runner import bind_snapshot, prune_cache


def response(rows):
    return SimpleNamespace(raise_for_status=lambda:None,
        json=lambda:{'code':0,'data':{'sz000001':{'qfqday':rows}}})


class ServerTests(unittest.TestCase):
    def test_cache_retention_only_removes_old_date_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name in ['2026-09-08','2026-09-09','2026-09-10','not-a-cache']:
                (root/name).mkdir()
            prune_cache(root,keep=2)
            self.assertFalse((root/'2026-09-08').exists())
            self.assertTrue((root/'2026-09-09').exists())
            self.assertTrue((root/'not-a-cache').exists())

    def test_stats_are_separate_and_histories_shared(self):
        snapshot = SimpleNamespace(frames={'a':object()},stats={'evaluated':9})
        a,b = SimpleNamespace(),SimpleNamespace()
        bind_snapshot(a,snapshot);bind_snapshot(b,snapshot)
        a.MARKET_DATA.stats['evaluated'] += 1
        self.assertEqual(b.MARKET_DATA.stats['evaluated'],0)
        self.assertEqual(snapshot.stats['evaluated'],9)
        self.assertIs(a.MARKET_DATA.frames,b.MARKET_DATA.frames)
        a.prepare_market_data()

    @patch('fast_qfq.session')
    def test_overlapping_windows_keep_exact_rows_and_volume(self,mock):
        first=['2022-12-30','1','2','3','1','100']
        second=['2024-12-30','2','3','4','2','200']
        last=['2026-09-09','3','4','5','3','300']
        mock.return_value.get.side_effect=[response([first]),response([first,second]),response([second,last])]
        frame=fast_qfq.full_history('sz000001','2026-09-09')
        self.assertEqual(len(frame),3)
        self.assertEqual(frame.vol.tolist(),[100,200,300])
        self.assertEqual(mock.return_value.get.call_count,3)

    @patch('fast_qfq.session')
    def test_conflicting_adjustments_rejected(self,mock):
        a=['2022-12-30','1','2','3','1','100']
        b=['2022-12-30','1','2.1','3','1','100']
        mock.return_value.get.side_effect=[response([a]),response([b]),response([])]
        with self.assertRaisesRegex(fast_qfq.MarketDataError,'basis differs'):
            fast_qfq.full_history('sz000001','2026-09-09')

    @patch('fast_qfq.session')
    def test_empty_history_rejected(self,mock):
        mock.return_value.get.return_value=response([])
        with self.assertRaisesRegex(fast_qfq.MarketDataError,'No Tencent'):
            fast_qfq.full_history('sz000001','2026-09-09')

    @patch('fast_qfq.session')
    def test_future_bars_rejected(self,mock):
        mock.return_value.get.return_value=response([['2026-09-10',1,2,3,1,10]])
        with self.assertRaisesRegex(fast_qfq.MarketDataError,'exceeds'):
            fast_qfq.full_history('sz000001','2026-09-09')


if __name__=='__main__':
    unittest.main()
