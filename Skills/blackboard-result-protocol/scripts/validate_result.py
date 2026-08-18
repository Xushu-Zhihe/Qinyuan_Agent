import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from Skills.result_protocol import validate_payload


def pair(value: str) -> tuple[str, str]:
    left, separator, right = value.partition(":")
    if not separator or not left or not right:
        raise ValueError("value must use left:right")
    return left, right


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intent-id", required=True)
    parser.add_argument("--goal-node-id", required=True)
    parser.add_argument("--existing-node", action="append", default=[])
    parser.add_argument("--existing-address", action="append", default=[])
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    try:
        source = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        payload = json.loads(source.strip())
        nodes = dict(pair(value) for value in args.existing_node)
        nodes.setdefault(args.goal_node_id, "goal")
        addresses = {pair(value) for value in args.existing_address}
        validate_payload(payload, intent_id=args.intent_id, goal_node_id=args.goal_node_id, existing_nodes=nodes, existing_addresses=addresses)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
