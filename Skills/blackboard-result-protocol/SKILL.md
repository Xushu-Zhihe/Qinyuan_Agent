---
name: blackboard-result-protocol
description: Use when executing a blackboard Intent and producing or correcting BLACKBOARD_RESULT v2 output.
---

# Blackboard Result Protocol v2

Finish with exactly one single-line `BLACKBOARD_RESULT: ` record followed by strict JSON. The object contains exactly `protocol_version`, `intent_id`, `facts`, `edges`, `goal_achieved`, and `goal_result_ref`.

Set `protocol_version` to `2`. Facts use `{"ref":"unique-ref","content":"non-empty fact"}`. Edges use `{"type":"addresses","from":"node-or-ref","to":"node-or-ref"}` and may use `supports`, `contradicts`, `derived_from`, `addresses`, `similar_to`, or `equivalent_to`.

When the Goal is achieved, `goal_result_ref` identifies one self-contained Fact that fully answers the current Goal. That Fact must directly `addresses` the current Goal, either through an existing graph edge or an edge in this result. Evidence Facts may support it but must not be selected as the result. When the Goal is not achieved, set `goal_result_ref` to `null`.

Before emitting the line, run:

`python <skill-directory>/scripts/validate_result.py --intent-id <id> --goal-node-id <goal-id> --existing-node <id>:<type> --existing-address <fact-id>:<goal-id> --file blackboard-result.json`

Examples:

```text
BLACKBOARD_RESULT: {"protocol_version":2,"intent_id":"intent-1","facts":[{"ref":"result","content":"The requested explanation is ..."}],"edges":[{"type":"addresses","from":"result","to":"goal-1"}],"goal_achieved":true,"goal_result_ref":"result"}
BLACKBOARD_RESULT: {"protocol_version":2,"intent_id":"intent-2","facts":[{"ref":"result","content":"{\"status\":\"complete\",\"count\":3}"}],"edges":[{"type":"addresses","from":"result","to":"goal-1"}],"goal_achieved":true,"goal_result_ref":"result"}
BLACKBOARD_RESULT: {"protocol_version":2,"intent_id":"intent-3","facts":[{"ref":"result","content":"Final report"},{"ref":"evidence","content":"Verified observation"}],"edges":[{"type":"addresses","from":"result","to":"goal-1"},{"type":"supports","from":"evidence","to":"result"}],"goal_achieved":true,"goal_result_ref":"result"}
```

Do not shorten the result to a flag or answer key. Do not wrap the protocol line in a Markdown fence.

Validation failures use the same precise parser vocabulary, including `fact proposal must be an object`, `unknown node reference`, and rejection of Python `key=value` syntax.
