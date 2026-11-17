"""Inspect effective launch options without starting a browser or meeting."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.browser import Browser, launch_sandboxed_chromium


class SandboxLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_browser_environment_selection_keeps_sandbox_enabled(self):
        browser = AsyncMock()
        chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
        playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
        with patch.dict(os.environ, {"MEETING_CHROMIUM": "/usr/bin/google-chrome"}, clear=True), \
                patch("playwright.async_api.async_playwright", return_value=starter):
            async with Browser():
                options = chromium.launch.await_args.kwargs
                self.assertEqual(options["executable_path"], "/usr/bin/google-chrome")
                self.assertIs(options["chromium_sandbox"], True)
                self.assertNotIn("ignore_default_args", options)
                self.assertNotIn("--no-sandbox", options["args"])
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_production_browser_explicit_sandbox_preserves_proxy_and_cleanup(self):
        browser = AsyncMock()
        chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
        playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://fixture-proxy.example:8080"}, clear=True), \
                patch("playwright.async_api.async_playwright", return_value=starter):
            async with Browser("/fixture/chromium"):
                options = chromium.launch.await_args.kwargs
                self.assertIs(options["chromium_sandbox"], True)
                self.assertEqual(options["executable_path"], "/fixture/chromium")
                self.assertEqual(options["proxy"], {"server": "http://fixture-proxy.example:8080"})
                self.assertTrue(options["headless"])
                self.assertNotIn("ignore_default_args", options)
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()

    async def test_sandbox_launch_failure_stops_driver_without_retry_or_downgrade(self):
        failure = RuntimeError("fixture sandbox unavailable")
        chromium = SimpleNamespace(launch=AsyncMock(side_effect=failure))
        playwright = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
        with patch.dict(os.environ, {}, clear=True), \
                patch("playwright.async_api.async_playwright", return_value=starter):
            with self.assertRaisesRegex(RuntimeError, "sandbox unavailable"):
                await Browser("/fixture/chromium").__aenter__()
        chromium.launch.assert_awaited_once()
        self.assertIs(chromium.launch.await_args.kwargs["chromium_sandbox"], True)
        playwright.stop.assert_awaited_once()

    async def test_sandbox_disabling_and_unknown_args_rejected_before_launch(self):
        chromium = SimpleNamespace(launch=AsyncMock())
        for argument in ("--no-sandbox", "--no-sandbox=false", "--disable-setuid-sandbox",
                         "--disable-seccomp-filter-sandbox", "--disable-namespace-sandbox",
                         "--disable-gpu-sandbox", "--single-process", "--unknown-option"):
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                await launch_sandboxed_chromium(chromium, args=[argument])
        for arguments in ("--no-sandbox", {"--no-sandbox": True}, [None], [True]):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await launch_sandboxed_chromium(chromium, args=arguments)
        chromium.launch.assert_not_awaited()

    async def test_sandbox_and_default_argument_overrides_are_not_supported(self):
        chromium = SimpleNamespace(launch=AsyncMock())
        for override in ({"chromium_sandbox": False}, {"ignore_default_args": True},
                         {"ignore_default_args": ["--enable-sandbox"]}):
            with self.subTest(override=override), self.assertRaises(TypeError):
                await launch_sandboxed_chromium(chromium, **override)
        chromium.launch.assert_not_awaited()

    async def test_each_launch_gets_fresh_arguments_and_loopback_proxy_is_preserved(self):
        chromium = SimpleNamespace(launch=AsyncMock())
        proxy = {"server": "http://127.0.0.1:12345", "bypass": "<-loopback>"}
        await launch_sandboxed_chromium(chromium, proxy=proxy)
        first = chromium.launch.await_args.kwargs
        first["args"].append("--no-sandbox")
        await launch_sandboxed_chromium(chromium, proxy=proxy)
        second = chromium.launch.await_args.kwargs
        self.assertIs(second["chromium_sandbox"], True)
        self.assertEqual(second["proxy"], proxy)
        self.assertNotIn("--no-sandbox", second["args"])

    def test_all_chromium_launch_sites_use_the_shared_policy(self):
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        sites = []
        for folder in ("teams_transcription", "experiments", "tools"):
            for path in (root / folder).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                            and node.func.attr in {"launch", "launch_persistent_context"}:
                        sites.append((path.relative_to(root).as_posix(), node.func.attr))
        self.assertEqual(sites, [("teams_transcription/browser.py", "launch")])
