import json
import subprocess
import unittest
from pathlib import Path

from used_pc_finder.ai_classifier import CodexCliClassifier
from used_pc_finder.models import Listing


def listing() -> Listing:
    return Listing(
        "RTX 4070 SUPER 팝니다",
        500000,
        "https://example.test/4070",
        "Haan-dong",
        "local",
        "4070",
        "정상 작동 확인했습니다.",
        "normal",
    )


def valid_result() -> dict[str, object]:
    return {
        "is_computer_part": True,
        "normalized_product_name": "RTX 4070 SUPER",
        "condition_status": "normal",
        "confidence": 0.98,
        "reject": False,
        "reason": "Complete, working graphics card.",
        "scope": "standalone",
    }


class AIClassifierTests(unittest.TestCase):
    schema_path = Path(__file__).parents[1] / "config" / "ai_listing_classification_schema.json"

    def fake_runner(self, result: dict[str, object], returncode: int = 0):
        def run(args, **_kwargs):
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(args, returncode, "", "failed" if returncode else "")

        return run

    def test_valid_json_output_is_parsed_and_cli_has_no_web_search_option(self):
        seen_args: list[str] = []

        def run(args, **kwargs):
            seen_args.extend(args)
            self.assertNotEqual(kwargs["cwd"], str(Path.cwd()))
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(valid_result()), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        classifier = CodexCliClassifier(
            self.schema_path, runner=run, timeout_seconds=1
        )
        result = classifier.classify(listing())

        self.assertIsNotNone(result)
        self.assertEqual(result.normalized_product_name, "RTX 4070 SUPER")
        self.assertEqual(classifier.calls, 1)
        self.assertIn("gpt-5.6-luna", seen_args)
        self.assertIn('model_reasoning_effort="low"', seen_args)
        self.assertNotIn("--search", seen_args)
        self.assertIn("Do not use tools", seen_args[-1])

    def test_timeout_fails_closed(self):
        def run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("codex", 1)

        result = CodexCliClassifier(self.schema_path, runner=run).classify(listing())
        self.assertIsNone(result)

    def test_subprocess_failure_fails_closed(self):
        result = CodexCliClassifier(
            self.schema_path, runner=self.fake_runner(valid_result(), returncode=1)
        ).classify(listing())
        self.assertIsNone(result)

    def test_invalid_json_fails_closed(self):
        result = CodexCliClassifier(
            self.schema_path, runner=self.fake_runner({"unexpected": True})
        ).classify(listing())
        self.assertIsNone(result)

    def test_unavailable_executable_fails_closed(self):
        def run(*_args, **_kwargs):
            raise OSError("Codex executable unavailable")

        result = CodexCliClassifier(self.schema_path, runner=run).classify(listing())
        self.assertIsNone(result)
