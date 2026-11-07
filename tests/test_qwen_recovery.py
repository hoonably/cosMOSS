"""Output-cap recovery without checkpoints, CUDA or personal recordings."""
import unittest

from src.speech_engines import transcribe_qwen_adaptively


def result(reason='stop', text='complete', ids=None):
    return text, dict(finish_reason=reason, token_ids=ids or [7, 9], raw=text)


class QwenRecoveryTests(unittest.TestCase):
    def run_parts(self, parts, infer, batch=2):
        self.events = []
        return transcribe_qwen_adaptively(parts, infer, lambda text: ('Korean', text),
                                         lambda **event: self.events.append(event), batch=batch, sample_rate=10)

    def test_only_capped_chunk_is_retried_and_clean_sibling_is_kept(self):
        calls = []
        audio = list(range(400))
        def infer(parts):
            calls.extend((offset, len(samples)) for samples, offset in parts)
            return [result('length' if offset == 0 and len(samples) == 400 else 'stop', str(offset))
                    for samples, offset in parts]
        chunks, retries = self.run_parts([(audio, 0), ([0]*100, 40)], infer)
        self.assertEqual(calls.count((40, 100)), 1)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]['reason'], 'length')
        self.assertTrue(all(c['finish_reason'] == 'stop' for c in chunks))
        self.assertEqual(chunks[0]['offset'], 0)
        self.assertEqual(chunks[-1]['end'], 50)
        for first, second in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(first['end'], second['offset'])
        self.assertEqual(self.events[-1], dict(chunk_count=len(chunks), completed=len(chunks)))
        self.assertTrue(any(event.get('retrying') for event in self.events))

    def test_repeated_splitting_covers_every_original_sample_once(self):
        audio, accepted = list(range(1600)), []
        def infer(parts):
            outputs = []
            for samples, offset in parts:
                if len(samples) > 220:
                    outputs.append(result('length'))
                else:
                    accepted.append((offset, samples))
                    outputs.append(result())
            return outputs
        chunks, retries = self.run_parts([(audio, 12.3)], infer, batch=4)
        self.assertGreater(len(retries), 2)
        self.assertEqual([sample for _, samples in sorted(accepted) for sample in samples], audio)
        self.assertAlmostEqual(chunks[0]['offset'], 12.3)
        self.assertAlmostEqual(chunks[-1]['end'], 172.3)
        for first, second in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(first['end'], second['offset'])

    def test_quiet_boundary_is_preferred_without_discarding_audio(self):
        audio = [1.0]*400
        audio[210:220] = [0.0]*10
        calls = []
        def infer(parts):
            calls.extend((len(samples), offset) for samples, offset in parts)
            return [result('length' if len(samples) == 400 else 'stop') for samples, _ in parts]
        chunks, _ = self.run_parts([(audio, 0)], infer)
        self.assertGreaterEqual(chunks[0]['end'], 21)
        self.assertLess(chunks[0]['end'], 22)
        self.assertAlmostEqual(sum(c['end']-c['offset'] for c in chunks), 40)

    def test_repeating_output_is_retried_instead_of_accepted(self):
        def infer(parts):
            return [result(ids=list(range(64))*6) if len(samples) == 400 else result()
                    for samples, _ in parts]
        chunks, retries = self.run_parts([([0]*400, 0)], infer)
        self.assertEqual(retries[0]['reason'], 'repetition')
        self.assertEqual(len(chunks), 2)

    def test_irrecoverable_short_chunk_stops_with_its_time_range(self):
        calls = []
        def infer(parts):
            calls.extend(parts)
            return [result('length') for _ in parts]
        with self.assertRaisesRegex(RuntimeError, 'Qwen 20.0–25.0초'):
            self.run_parts([([0]*100, 20)], infer)
        self.assertEqual(len(calls), 3)

    def test_missing_batch_output_cannot_silently_drop_a_chunk(self):
        with self.assertRaisesRegex(RuntimeError, '출력 개수'):
            self.run_parts([([0]*100, 0), ([0]*100, 10)], lambda _: [result()])

    def test_clean_and_empty_transcripts_do_not_trigger_retries(self):
        chunks, retries = self.run_parts([([0]*100, 0), ([0]*100, 10)],
                                        lambda _: [result(text=''), result(text='spoken')])
        self.assertFalse(retries)
        self.assertEqual([chunk['text'] for chunk in chunks], ['', 'spoken'])
