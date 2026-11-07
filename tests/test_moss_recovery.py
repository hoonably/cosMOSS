"""Recover truncated full-context output without losing the accepted prefix."""
import unittest

from src.speech_engines import moss_segments, missing_moss_tail, transcribe_moss_continuations


class MossRecoveryTests(unittest.TestCase):
    def test_partial_timestamp_is_never_accepted_as_complete(self):
        with self.assertRaisesRegex(RuntimeError, '중간에서 끊겼습니다'):
            moss_segments('[0.00][S01] complete[3077.57][3077', 3306.09)

    def test_55_minute_recording_resumes_from_last_complete_segment(self):
        calls, events = [], []
        def infer(offset, maximum):
            calls.append((offset, maximum))
            if offset == 0:
                return {'text':'[0.00][S01] prefix[3077.57][3077'}
            return {'text':'[0.00][S02] recovered tail[228.52]'}
        items, chunks = transcribe_moss_continuations(3306.09, infer, lambda **event: events.append(event))
        self.assertEqual([offset for offset, _ in calls], [0, 3077.57])
        self.assertEqual(calls[0][1], 65536)
        self.assertEqual([row['text'] for row in items], ['prefix', 'recovered tail'])
        self.assertAlmostEqual(items[-1]['end'], 3306.09)
        self.assertEqual(items[0]['end'], items[1]['start'])
        self.assertTrue(chunks[0]['truncated'])
        self.assertFalse(chunks[1]['truncated'])
        self.assertTrue(events[-1]['retrying'])

    def test_explicit_length_reason_resumes_even_with_closed_markup(self):
        responses=iter([{'text':'[0][S01] first[20]','finish_reason':'length'},
                        {'text':'[0][S01] second[10]'}])
        items, _ = transcribe_moss_continuations(30,lambda *_: next(responses),lambda **_: None)
        self.assertEqual(items[-1]['end'],30)

    def test_valid_short_transcript_with_silent_tail_is_preserved(self):
        items, chunks = transcribe_moss_continuations(30,
            lambda *_:{'text':'[0][S01] speech[20]'},lambda **_: None)
        self.assertEqual(len(chunks),1)
        self.assertIsNone(missing_moss_tail(items,[{'start':0,'end':20}]))

    def test_diarization_detects_a_spoken_tail_after_natural_eos(self):
        self.assertEqual(missing_moss_tail([{'end':3077.57}],[{'start':3280,'end':3306.09}]),3077.57)
        self.assertIsNone(missing_moss_tail([{'end':3305}],[{'start':3280,'end':3306.09}]))

    def test_no_progress_or_endless_partial_output_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError,'재시작할 위치'):
            transcribe_moss_continuations(30,lambda *_:{'text':'[0][S01] tiny[0.1][0'},lambda **_: None)
        with self.assertRaisesRegex(RuntimeError,'재시도 횟수'):
            transcribe_moss_continuations(30,lambda *_:{'text':'[0][S01] step[5][5'},lambda **_: None,max_attempts=2)

    def test_resume_offset_is_added_to_every_timestamp(self):
        items,chunks=transcribe_moss_continuations(60,lambda *_:{'text':'[2][S01] suffix[10]'},
            lambda **_:None,start_offset=40)
        self.assertEqual((items[0]['start'],items[0]['end']),(42,50))
        self.assertEqual(chunks[0]['offset'],40)
