"""Application framing from synthetic capture packets, never provider finals."""
import struct
import unittest

from teams_transcription.b_question import ApplicationQuestionFramer, FramedQuestion


class Capture:
    """One 50ms capture packet per tick on the injectable monotonic clock."""

    def __init__(self, **options):
        self.now = 0.0
        self.framer = ApplicationQuestionFramer(clock=lambda: self.now, **options)

    def packet(self, amplitude=0):
        start = self.now
        self.now += .05
        return self.framer.push_pcm(struct.pack('<h', amplitude) * 1200,
                                   started_at=start, ended_at=self.now)

    def quiet(self, packets=30):
        for _ in range(packets):
            self.packet()

    def say(self, text):
        started = self.packet(1000)
        self.framer.append_transcript(text)
        return started


class ApplicationQuestionTests(unittest.TestCase):
    def test_question_requires_observed_quiet_and_is_emitted_once(self):
        capture = Capture()
        self.assertTrue(capture.say('小白小白，這次預算是多少？'))
        capture.quiet(29)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertEqual(capture.framer.poll(),
                         FramedQuestion('question', '這次預算是多少', 1))
        self.assertIsNone(capture.framer.poll())
        self.assertEqual(capture.framer.pending_status(),
                         {'characters': 0, 'has_wake': False, 'has_question': False})

    def test_long_spoken_question_preserves_tail_and_emits_once(self):
        capture = Capture()
        question = '請依序核對以下事項:' + '逐項確認資料。' * 400 + '最後請回覆交付日期與負責人'
        capture.say('小白小白，' + question[:1000])
        capture.framer.append_transcript(question[1000:2000])
        capture.framer.append_transcript(question[2000:])
        capture.quiet()
        request = capture.framer.poll()
        self.assertIsNotNone(request)
        self.assertEqual(request, FramedQuestion('question', question, 1))
        self.assertIsNone(capture.framer.poll())


    def test_wall_time_without_capture_is_not_silence(self):
        capture = Capture()
        capture.say('小白小白，問題一？')
        capture.now += 10
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(29)
        self.assertEqual(capture.framer.poll(), FramedQuestion('question', '問題一', 1))

    def test_gap_does_not_join_two_short_quiet_runs(self):
        capture = Capture()
        capture.say('小白小白，問題一')
        capture.quiet(20)
        capture.now += .5
        capture.quiet(10)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(20)
        self.assertEqual(capture.framer.poll().text, '問題一')

    def test_callback_jitter_is_tolerated_but_not_counted_as_quiet(self):
        capture = Capture()
        capture.say('小白小白，問題一')
        for _ in range(29):
            capture.now += .01
            capture.packet()
        self.assertIsNone(capture.framer.poll())
        capture.packet()
        self.assertEqual(capture.framer.poll().text, '問題一')

    def test_completed_quiet_must_still_be_recent_when_polled(self):
        capture = Capture()
        capture.say('小白小白，問題一')
        capture.quiet()
        capture.now += .251
        self.assertIsNone(capture.framer.poll())
        capture.quiet(29)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertEqual(capture.framer.poll().sequence, 1)

    def test_replayed_or_out_of_order_packets_cannot_supply_quiet(self):
        capture = Capture()
        capture.say('小白小白，問題一')
        capture.quiet(1)
        for _ in range(100):
            capture.framer.push_pcm(b'\0\0' * 1200, started_at=.05, ended_at=.1)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(29)
        self.assertEqual(capture.framer.poll().sequence, 1)

    def test_no_voice_and_low_nonzero_noise_cannot_authorize_text(self):
        for amplitude in (0, 1, 8, 255):
            with self.subTest(amplitude=amplitude):
                capture = Capture()
                self.assertFalse(capture.packet(amplitude))
                capture.framer.append_transcript('小白小白，問題一')
                capture.quiet()
                self.assertIsNone(capture.framer.poll())
                self.assertEqual(capture.framer.pending_status()['characters'], 0)

    def test_input_activity_threshold_and_quiet_noise_floor(self):
        capture = Capture()
        self.assertTrue(capture.packet(256))
        capture.framer.append_transcript('小白小白，問題一')
        for _ in range(30):
            capture.packet(255)
        self.assertEqual(capture.framer.poll().text, '問題一')

    def test_unwoken_speech_is_discarded_and_cannot_seed_next_request(self):
        capture = Capture()
        capture.say('不含喚醒詞的會議討論')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())
        self.assertEqual(capture.framer.pending_status()['characters'], 0)
        capture.say('小白小白，問題一')
        capture.quiet()
        self.assertEqual(capture.framer.poll(), FramedQuestion('question', '問題一', 1))

    def test_delayed_delta_requires_its_own_settle_time_and_fresh_capture(self):
        capture = Capture()
        capture.packet(1000)
        capture.quiet(40)
        self.assertIsNone(capture.framer.poll())
        capture.framer.append_transcript('小白小白，問題一')
        capture.quiet(11)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertEqual(capture.framer.poll().text, '問題一')

    def test_new_delta_restarts_text_settle_not_audio_quiet(self):
        capture = Capture()
        capture.say('小白小白，預算')
        capture.quiet(29)
        capture.framer.append_transcript('是多少？')
        capture.quiet(11)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertEqual(capture.framer.poll().text, '預算是多少')

    def test_short_pause_continues_one_question_without_early_greeting(self):
        capture = Capture()
        self.assertTrue(capture.say('小白小白'))
        self.assertFalse(capture.packet(1000))
        capture.quiet(20)
        self.assertIsNone(capture.framer.poll())
        self.assertTrue(capture.say('，這次預算是多少？'))
        capture.quiet(29)
        self.assertIsNone(capture.framer.poll())
        capture.quiet(1)
        self.assertEqual(capture.framer.poll(),
                         FramedQuestion('question', '這次預算是多少', 1))

    def test_identical_questions_are_new_requests_without_provider_finals(self):
        capture = Capture()
        for sequence in range(1, 4):
            capture.say('小白小白，問題一')
            capture.quiet()
            self.assertEqual(capture.framer.poll(),
                             FramedQuestion('question', '問題一', sequence))

    def test_bare_wake_does_not_implicitly_arm_followup(self):
        capture = Capture()
        capture.say('小白小白！')
        capture.quiet()
        self.assertEqual(capture.framer.poll(), FramedQuestion('greeting', '小白小白', 1))
        capture.say('這次預算是多少？')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())

    def test_completed_greeting_explicitly_arms_one_followup(self):
        capture = Capture()
        capture.say('小白小白')
        capture.quiet()
        self.assertEqual(capture.framer.poll().kind, 'greeting')
        capture.framer.arm_followup()
        capture.say('這次預算是多少？')
        self.assertEqual(capture.framer.pending_status(),
                         {'characters': 8, 'has_wake': False, 'has_question': True})
        capture.quiet()
        self.assertEqual(capture.framer.poll(),
                         FramedQuestion('question', '這次預算是多少', 2))
        capture.say('還有其他資料嗎？')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())

    def test_followup_expiry_is_checked_at_speech_start(self):
        capture = Capture(followup_seconds=1)
        capture.framer.arm_followup()
        capture.now = .95
        capture.say('最後期限前開始的問題')
        capture.quiet()
        self.assertEqual(capture.framer.poll().text, '最後期限前開始的問題')
        capture.framer.arm_followup()
        capture.now += 1
        capture.say('最後期限後開始的問題')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())

    def test_reset_cancels_pending_and_followup_without_reusing_sequence(self):
        capture = Capture()
        capture.say('小白小白')
        capture.quiet()
        self.assertEqual(capture.framer.poll().sequence, 1)
        capture.framer.arm_followup()
        capture.say('取消的問題')
        capture.framer.reset()
        capture.quiet()
        capture.framer.append_transcript('遲到的逐字')
        self.assertIsNone(capture.framer.poll())
        capture.say('沒有再次喚醒的問題')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())
        capture.say('小白小白，新的問題')
        capture.quiet()
        self.assertEqual(capture.framer.poll(), FramedQuestion('question', '新的問題', 2))


    def test_wake_alias_nfkc_casefold_punctuation_and_ascii_boundaries(self):
        for alias, utterance, question in (
                ('小白小白', '小白、小白：問題一？', '問題一'),
                ('hello bot', 'ＨＥＬＬＯ  ＢＯＴ：Budget？', 'budget')):
            with self.subTest(alias=alias):
                capture = Capture(wake_words=(alias,))
                capture.say(utterance)
                capture.quiet()
                self.assertEqual(capture.framer.poll().text, question)
        capture = Capture(wake_words=('bot',))
        capture.say('robot，這個不是喚醒')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())

    def test_status_has_application_provenance_without_transcript_content(self):
        capture = Capture()
        capture.say('小白小白，私有問題')
        status = capture.framer.status()
        self.assertEqual(status['provenance'], 'application')
        self.assertFalse(status['provider_final'])
        self.assertFalse(status['source_audio_verified'])
        self.assertEqual(status['activity_detector'], 'pcm_rms_heuristic')
        self.assertNotIn('私有問題', str(status))
        capture.quiet(100)
        self.assertEqual(capture.framer.status()['observed_quiet_seconds'], 1.5)

    def test_text_before_activity_or_after_emission_cannot_authorize_request(self):
        capture = Capture()
        capture.framer.append_transcript('小白小白，沒有音訊')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())
        capture.say('小白小白，真的問題')
        capture.quiet()
        self.assertEqual(capture.framer.poll().sequence, 1)
        capture.framer.append_transcript('小白小白，遲到的文字')
        capture.quiet()
        self.assertIsNone(capture.framer.poll())
        self.assertEqual(capture.framer.pending_status()['characters'], 0)

