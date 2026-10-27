"""离线测试：不需要 API key，验证循环、工具边界与停止条件。"""
import unittest
from pathlib import Path

from agent import AgentLoopError, demo_script, run_agent
from llm import FakeLLM
from tools import build_registry

SANDBOX = Path(__file__).resolve().parent / "sandbox"


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(SANDBOX)

    def test_demo_runs_two_tools_and_finishes(self):
        result = run_agent(
            "端口是多少？", FakeLLM(demo_script()), self.registry, verbose=False
        )
        self.assertIn("8081", result["answer"])
        tool_steps = [step for step in result["trace"] if step["type"] == "tool"]
        self.assertEqual(
            [step["name"] for step in tool_steps], ["list_files", "read_file"]
        )

    def test_max_steps_guard(self):
        looping = FakeLLM(
            [
                {
                    "content": "再看一次",
                    "tool_calls": [
                        {"id": "call_%d" % i, "name": "list_files", "arguments": {}}
                    ],
                }
                for i in range(5)
            ]
        )
        with self.assertRaises(AgentLoopError):
            run_agent("x", looping, self.registry, max_steps=3, verbose=False)

    def test_path_traversal_is_blocked(self):
        result = self.registry.execute("read_file", {"path": "../agent.py"})
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("sandbox", result)

    def test_calculator(self):
        result = self.registry.execute("calculator", {"expression": "(23 * 17) + 4"})
        self.assertIn("395", result)

    def test_calculator_rejects_code(self):
        result = self.registry.execute(
            "calculator", {"expression": "__import__('os').system('echo pwned')"}
        )
        self.assertTrue(result.startswith("ERROR"))

    def test_unknown_tool_returns_error_text(self):
        result = self.registry.execute("drop_database", {})
        self.assertTrue(result.startswith("ERROR: unknown tool"))

    def test_bad_arguments_return_error_text(self):
        result = self.registry.execute("read_file", {})
        self.assertTrue(result.startswith("ERROR: bad arguments"))


if __name__ == "__main__":
    unittest.main()
