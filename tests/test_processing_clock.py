import unittest
from datetime import UTC, datetime, timedelta
from src.processing_clock import advance_stage, stage_elapsed

class ProcessingClockTests(unittest.TestCase):
    def test_loading_and_work_are_accumulated_separately_across_model_changes(self):
        start=datetime(2026,11,10,tzinfo=UTC)
        timing=advance_stage({},'loading',start)
        self.assertEqual(stage_elapsed(timing,start+timedelta(seconds=12)),{'loading_seconds':12,'processing_seconds':0})
        timing=advance_stage(timing,'processing',start+timedelta(seconds=20))
        timing=advance_stage(timing,'loading',start+timedelta(seconds=50))
        timing=advance_stage(timing,'processing',start+timedelta(seconds=55))
        timing=advance_stage(timing,None,start+timedelta(seconds=65))
        self.assertEqual(stage_elapsed(timing,start+timedelta(seconds=1000)),{'loading_seconds':25,'processing_seconds':40})
        self.assertIsNone(stage_elapsed(None))
