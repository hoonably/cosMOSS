"""Context recommendations are scoped, balanced, and never submission gates."""
import unittest
from unittest.mock import patch
from src.moss_context import recommend_moss_context
from src.speech_engines import ENGINE_VERSION


class MossContextTests(unittest.TestCase):
    def setUp(self):
        self.profile={'memory_knots':[[300,2],[5400,14]],'time_knots':[[300,1],[5400,90]],
                      'memory_multiplier':1.1,'memory_headroom_gib':1}
        self.config={'profiles':{'moss-sglang':self.profile}}

    def estimate(self,capacity,free=None,measured=False,observations=()):
        gpu={'name':'NVIDIA Fixture GPU','memory_total_mib':capacity*1024}
        if free is not None:gpu['memory_free_mib']=free*1024
        with patch('src.moss_context.calibrations',return_value=self.config), \
             patch('src.moss_context.server_profile',return_value=self.profile if measured else None):
            return recommend_moss_context(gpu,observations)

    def test_lower_capacity_shortens_only_initial_context_and_respects_free_memory(self):
        values=[self.estimate(capacity) for capacity in [4,8,12,24,48,140]]
        self.assertEqual([v['seconds'] for v in values],sorted(v['seconds'] for v in values))
        self.assertEqual(values[-1]['seconds'],5400)
        self.assertLess(self.estimate(48,free=8)['seconds'],self.estimate(48)['seconds'])
        self.assertEqual(self.estimate(1)['seconds'],300)
        self.assertFalse(self.estimate(1)['fits'])

    def test_known_hardware_uses_its_own_curve_instead_of_generic_memory(self):
        self.assertFalse(self.estimate(12)['measured'])
        self.assertTrue(self.estimate(12,measured=True)['measured'])

    def test_failed_long_validation_can_cap_default_and_success_can_extend_it(self):
        self.config['moss_context_validation']=[{'gpu_name':'fixture gpu','capacity_gib':24,
            'runtime':ENGINE_VERSION,'max_verified_seconds':1800}]
        self.assertEqual(self.estimate(24)['seconds'],1800)
        success=dict(pipeline='moss-sglang',runtime=ENGINE_VERSION,gpu_name='fixture gpu',capacity_gib=24,
                     batch=1,moss_partition_count=1,audio_seconds=3600,peak_process_gib=8)
        self.assertEqual(self.estimate(24,observations=[success])['seconds'],3600)
        self.assertEqual(self.estimate(48)['seconds'],5400)
        self.assertEqual(self.estimate(24,observations=[{**success,'gpu_name':'other gpu'}])['seconds'],1800)

    def test_unknown_capacity_does_not_invent_a_memory_measurement(self):
        self.assertIsNone(recommend_moss_context({})['fits'])
        self.assertFalse(recommend_moss_context({})['measured'])

    def test_observed_3090_engine_rejection_shortens_context_without_blocking_job(self):
        from src.speech_pipeline import estimate_pipeline
        gpu={'name':'NVIDIA GeForce RTX 3090','memory_total_mib':24576}
        plan=estimate_pipeline('moss-sglang',5400,gpu)
        self.assertEqual(plan['moss_partition_count'],2)
        self.assertEqual(plan['chunk_seconds'],2700)
        self.assertEqual(plan['moss_chunk_seconds'],4800)
        self.assertLess(plan['moss_context_recommendation']['engine_input_limit_tokens'],71687)
        # Existing H200/Ada completions must retain their 90-minute context.
        for name,capacity in [('NVIDIA H200',143771),('NVIDIA RTX 6000 Ada Generation',49140)]:
            self.assertEqual(recommend_moss_context({'name':name,'memory_total_mib':capacity})['seconds'],5400)
