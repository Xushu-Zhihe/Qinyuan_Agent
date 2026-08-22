import unittest

from Skills.result_protocol import validate_payload


class ResultProtocolBudgetTests(unittest.TestCase):
    def validate(self, payload: dict) -> None:
        validate_payload(
            payload,
            intent_id="intent-1",
            goal_node_id="goal-1",
            existing_nodes={"goal-1": "goal"},
            existing_addresses=set(),
        )

    @staticmethod
    def partial(facts: list[dict]) -> dict:
        return {
            "protocol_version": 2,
            "intent_id": "intent-1",
            "facts": facts,
            "edges": [],
            "goal_achieved": False,
            "goal_result_ref": None,
        }

    def test_rejects_more_than_four_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than 4 facts"):
            self.validate(self.partial([
                {"ref": f"fact-{index}", "content": "verified"}
                for index in range(5)
            ]))

    def test_rejects_long_evidence_fact(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds 800 characters"):
            self.validate(self.partial([
                {"ref": "evidence", "content": "x" * 801},
            ]))

    def test_allows_larger_goal_result_fact(self) -> None:
        self.validate({
            "protocol_version": 2,
            "intent_id": "intent-1",
            "facts": [{"ref": "result", "content": "x" * 4000}],
            "edges": [
                {"type": "addresses", "from": "result", "to": "goal-1"},
            ],
            "goal_achieved": True,
            "goal_result_ref": "result",
        })

    def test_rejects_total_fact_budget_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "total fact content exceeds 6000"):
            self.validate({
                "protocol_version": 2,
                "intent_id": "intent-1",
                "facts": [
                    {"ref": "result", "content": "x" * 4000},
                    {"ref": "evidence-1", "content": "y" * 800},
                    {"ref": "evidence-2", "content": "z" * 800},
                    {"ref": "evidence-3", "content": "w" * 401},
                ],
                "edges": [
                    {"type": "addresses", "from": "result", "to": "goal-1"},
                ],
                "goal_achieved": True,
                "goal_result_ref": "result",
            })


if __name__ == "__main__":
    unittest.main()
