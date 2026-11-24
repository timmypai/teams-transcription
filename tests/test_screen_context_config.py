"""Configuration seam for Phase 1 (on_demand) and Phase 2 (watch_and_cache) screen context."""
import tempfile
import unittest
from pathlib import Path

from teams_transcription.assistant_config import AssistantSettings, load_assistant_settings


class ScreenContextConfigTests(unittest.TestCase):
    def _base(self, root):
        return {
            'data_root': root,
            'admin_password_file': str(Path(root) / 'password.json'),
            'admin_session_secret_file': str(Path(root) / 'session.key'),
            'google_credentials_file': str(Path(root) / 'google.json'),
        }

    def test_defaults_opt_in_off_with_bounded_parameters(self):
        with tempfile.TemporaryDirectory() as root:
            settings = AssistantSettings(**self._base(root))
            self.assertEqual(settings.screen_context_mode, 'off')
            self.assertIsNone(settings.vision_model)
            self.assertEqual(settings.screen_poll_seconds, 2.5)
            self.assertEqual(settings.screen_min_analyze_interval_seconds, 6.0)
            self.assertEqual(settings.screen_frame_cache_limit, 16)
            self.assertTrue(settings.screen_include_in_summary)

    def test_accepts_phase1_on_demand_and_phase2_watch_and_cache_modes(self):
        with tempfile.TemporaryDirectory() as root:
            p1 = AssistantSettings(**self._base(root), screen_context_mode='on_demand',
                vision_model='gpt-5.6-sol', screen_include_in_summary=False)
            self.assertEqual(p1.screen_context_mode, 'on_demand')
            self.assertEqual(p1.vision_model, 'gpt-5.6-sol')
            self.assertFalse(p1.screen_include_in_summary)

            p2 = AssistantSettings(**self._base(root), screen_context_mode='watch_and_cache',
                screen_poll_seconds=3.0, screen_min_analyze_interval_seconds=8.0,
                screen_frame_cache_limit=24)
            self.assertEqual(p2.screen_context_mode, 'watch_and_cache')
            self.assertEqual(p2.screen_poll_seconds, 3.0)
            self.assertEqual(p2.screen_min_analyze_interval_seconds, 8.0)
            self.assertEqual(p2.screen_frame_cache_limit, 24)

    def test_rejects_invalid_mode_live_vision_model_and_non_bool_summary_flag(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                AssistantSettings(**self._base(root), screen_context_mode='realtime_video')
            with self.assertRaises(ValueError):
                AssistantSettings(**self._base(root), vision_model='gpt-live-1-codex')
            with self.assertRaises(ValueError):
                AssistantSettings(**self._base(root), screen_include_in_summary='true')
            with self.assertRaises(ValueError):
                AssistantSettings(**self._base(root), screen_poll_seconds=0.1)
            with self.assertRaises(ValueError):
                AssistantSettings(**self._base(root), screen_frame_cache_limit=0)

    def test_env_and_yaml_precedence_loads_screen_context_mode(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / 'runtime.yml'
            base = self._base(root)
            cfg.write_text(
                f"data_root: {base['data_root']}\n"
                f"admin_password_file: {base['admin_password_file']}\n"
                f"admin_session_secret_file: {base['admin_session_secret_file']}\n"
                f"google_credentials_file: {base['google_credentials_file']}\n"
                "screen_context_mode: on_demand\n",
                encoding='utf-8',
            )
            from_yaml = load_assistant_settings(cfg, environ={})
            self.assertEqual(from_yaml.screen_context_mode, 'on_demand')
            from_env = load_assistant_settings(
                cfg,
                environ={'ASSISTANT_SCREEN_CONTEXT_MODE': 'watch_and_cache',
                         'ASSISTANT_SCREEN_INCLUDE_IN_SUMMARY': 'false'},
            )
            self.assertEqual(from_env.screen_context_mode, 'watch_and_cache')
            self.assertFalse(from_env.screen_include_in_summary)


if __name__ == '__main__':
    unittest.main()
