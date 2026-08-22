"""Vendored BLACKBOARD_RESULT v2 validator; no cross-repository imports."""
RESULT_FIELDS = {"protocol_version", "intent_id", "facts", "edges", "goal_achieved", "goal_result_ref"}
FACT_FIELDS = {"ref", "content"}
EDGE_FIELDS = {"type", "from", "to"}
EDGE_TYPES = {"supports", "contradicts", "derived_from", "addresses", "similar_to", "equivalent_to"}
MAX_FACTS = 4
MAX_FACT_LENGTH = 800
MAX_RESULT_FACT_LENGTH = 4000
MAX_TOTAL_FACT_LENGTH = 6000


def validate_payload(payload, *, intent_id, goal_node_id, existing_nodes, existing_addresses):
    if not isinstance(payload, dict) or set(payload) != RESULT_FIELDS:
        raise ValueError("result fields must be exactly protocol_version, intent_id, facts, edges, goal_achieved, goal_result_ref")
    if payload["protocol_version"] != 2:
        raise ValueError("protocol_version must be 2")
    if payload["intent_id"] != intent_id:
        raise ValueError("result must reference the assigned intent")
    if not isinstance(payload["facts"], list) or not isinstance(payload["edges"], list):
        raise ValueError("facts and edges must be lists")
    if len(payload["facts"]) > MAX_FACTS:
        raise ValueError(f"result contains more than {MAX_FACTS} facts")
    refs = set()
    fact_contents = {}
    for fact in payload["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("fact proposal must be an object")
        if set(fact) != FACT_FIELDS:
            raise ValueError("fact fields must be exactly ref and content")
        ref, content = fact["ref"], fact["content"]
        if not isinstance(ref, str) or not ref.strip() or ref in refs:
            raise ValueError("fact ref must be a unique non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("fact content must be a non-empty string")
        refs.add(ref)
        fact_contents[ref] = content.strip()
    if sum(len(content) for content in fact_contents.values()) > MAX_TOTAL_FACT_LENGTH:
        raise ValueError(f"total fact content exceeds {MAX_TOTAL_FACT_LENGTH} characters")
    known = set(existing_nodes) | refs
    edges = set()
    for edge in payload["edges"]:
        if not isinstance(edge, dict) or set(edge) != EDGE_FIELDS:
            raise ValueError("edge fields must be exactly type, from, and to")
        edge_type, source, target = edge["type"], edge["from"], edge["to"]
        if edge_type not in EDGE_TYPES:
            raise ValueError("unsupported relation edge type")
        if source not in known or target not in known:
            raise ValueError("unknown node reference")
        edges.add((edge_type, source, target))
    if not isinstance(payload["goal_achieved"], bool):
        raise ValueError("goal_achieved must be a boolean")
    achieved, result_ref = payload["goal_achieved"], payload["goal_result_ref"]
    if not achieved:
        for content in fact_contents.values():
            if len(content) > MAX_FACT_LENGTH:
                raise ValueError(f"fact content exceeds {MAX_FACT_LENGTH} characters")
        if result_ref is not None:
            raise ValueError("goal_result_ref must be null when goal is not achieved")
        return
    if not isinstance(result_ref, str) or not result_ref.strip():
        raise ValueError("goal_achieved requires goal_result_ref")
    for ref, content in fact_contents.items():
        maximum = MAX_RESULT_FACT_LENGTH if ref == result_ref else MAX_FACT_LENGTH
        if len(content) > maximum:
            label = "goal result Fact" if ref == result_ref else "fact"
            raise ValueError(f"{label} content exceeds {maximum} characters")
    if result_ref not in refs and existing_nodes.get(result_ref) != "fact":
        raise ValueError("goal_result_ref must reference a Fact")
    if (result_ref, goal_node_id) not in existing_addresses and ("addresses", result_ref, goal_node_id) not in edges:
        raise ValueError("goal result must address the current Goal")
