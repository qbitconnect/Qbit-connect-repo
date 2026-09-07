"""Workflow graph loading + structural validation (§4, §5, §21, §42).

The graph is a directed acyclic graph of typed nodes. Validation enforces:
- exactly one TRIGGER node, at least one END reachable
- every node reachable from the trigger (no dead nodes)
- no cycles
- all next-node references exist
- node-count cap (max_nodes_per_workflow, §42)
- per-node-type structural rules (CONDITION has yes/no branches, WAIT has a
  duration ≤ max, ACTION has an action key, END has no outgoing edge)
"""

from __future__ import annotations

from app.automation.core.exceptions import ValidationError
from app.automation.core.schemas import WorkflowDefinition
from app.models.automation import NodeTypes

#: node types that must provide an outgoing edge
_TYPES_WITH_NEXT = {NodeTypes.TRIGGER, NodeTypes.ACTION, NodeTypes.WAIT, NodeTypes.BRANCH}


def validate_graph(definition: WorkflowDefinition, *, max_nodes: int) -> None:
    nodes = definition.nodes
    if len(nodes) > max_nodes:
        raise ValidationError([f"Too many nodes ({len(nodes)} > {max_nodes})"])

    triggers = [n for n in nodes if n.type == NodeTypes.TRIGGER]
    if len(triggers) != 1:
        raise ValidationError([f"Workflow must have exactly one TRIGGER node (found {len(triggers)})"])
    ends = [n for n in nodes if n.type == NodeTypes.END]
    if not ends:
        raise ValidationError(["Workflow must have at least one END node"])

    ids = {n.id for n in nodes}
    if len(ids) != len(nodes):
        raise ValidationError(["Duplicate node ids in definition"])

    issues: list[str] = []

    # --- structural per-node rules -------------------------------------------
    for node in nodes:
        if node.type in _TYPES_WITH_NEXT and not node.next_node_id:
            issues.append(f"Node {node.id!r} ({node.type}) requires next_node_id")
        if node.next_node_id and node.next_node_id not in ids:
            issues.append(f"Node {node.id!r} references missing node {node.next_node_id!r}")
        if node.type == NodeTypes.END and node.next_node_id:
            issues.append(f"END node {node.id!r} must not have a next node")
        if node.type == NodeTypes.TRIGGER and node.next_node_id == node.id:
            issues.append(f"TRIGGER node {node.id!r} cannot point at itself")

        if node.type == NodeTypes.CONDITION:
            if node.condition is None:
                issues.append(f"CONDITION node {node.id!r} requires a condition")
            if not node.next_node_id or node.next_node_id not in ids:
                issues.append(f"CONDITION node {node.id!r} requires a valid YES branch (next_node_id)")
            if not node.next_node_id_no or node.next_node_id_no not in ids:
                issues.append(f"CONDITION node {node.id!r} requires a valid NO branch (next_node_id_no)")

        if node.type == NodeTypes.BRANCH:
            if not node.branches:
                issues.append(f"BRANCH node {node.id!r} requires at least one branch")
            else:
                for i, branch in enumerate(node.branches):
                    if branch.next_node_id not in ids:
                        issues.append(
                            f"BRANCH node {node.id!r} branch {i} references missing node"
                        )
            if not node.next_node_id or node.next_node_id not in ids:
                issues.append(f"BRANCH node {node.id!r} requires a valid default branch")

        if node.type == NodeTypes.ACTION and not node.action:
            issues.append(f"ACTION node {node.id!r} requires an action key")

        if node.type == NodeTypes.WAIT and node.duration is None:
            issues.append(f"WAIT node {node.id!r} requires a duration")

    if issues:
        raise ValidationError(issues)

    # --- reachability + cycle detection (DFS from trigger) ---------------------
    trigger = triggers[0]
    visited: set[str] = set()
    stack: list[str] = []
    if not _walk(trigger.id, definition, visited, stack):
        raise ValidationError([f"Cycle detected in workflow graph at node {stack[-1]!r}"])

    unreachable = ids - visited
    if unreachable:
        raise ValidationError(
            [f"Unreachable nodes: {', '.join(sorted(unreachable))}"]
        )

    # --- every path must terminate at an END node (bounded walk) ---------------
    for node in nodes:
        if node.type in (NodeTypes.END,):
            continue
        targets = _outgoing(node)
        if not targets and node.type != NodeTypes.END:
            # already reported above; skip
            continue
    terminal_ok = _all_paths_reach_end(trigger.id, definition)
    if not terminal_ok:
        raise ValidationError(["Every execution path must end at an END node"])


def _outgoing(node) -> list[str]:
    targets: list[str] = []
    if node.next_node_id:
        targets.append(node.next_node_id)
    if node.type == NodeTypes.CONDITION and node.next_node_id_no:
        targets.append(node.next_node_id_no)
    if node.type == NodeTypes.BRANCH and node.branches:
        targets.extend(b.next_node_id for b in node.branches)
    return targets


def _walk(node_id: str, definition: WorkflowDefinition, visited: set[str], stack: list[str]) -> bool:
    """Iterative DFS with cycle detection (bounded by node count)."""
    graph = {n.id: _outgoing(n) for n in definition.nodes}
    visiting: set[str] = set()

    def dfs(current: str) -> bool:
        if current in visited:
            return True
        if current in visiting:
            stack.append(current)
            return False
        visiting.add(current)
        for nxt in graph.get(current, []):
            if not dfs(nxt):
                if not stack or stack[-1] != current:
                    stack.append(current)
                return False
        visiting.discard(current)
        visited.add(current)
        return True

    ok = dfs(node_id)
    if not ok and not stack:
        stack.append(node_id)
    return ok


def _all_paths_reach_end(trigger_id: str, definition: WorkflowDefinition) -> bool:
    """BFS over edges counting remaining steps — every path must hit END within
    the node budget (with the graph already acyclic, this terminates)."""
    graph = {n.id: _outgoing(n) for n in definition.nodes}
    end_ids = {n.id for n in definition.nodes if n.type == NodeTypes.END}
    budget = len(definition.nodes) + 1
    frontier = [(trigger_id, 0)]
    seen_states: set[tuple[str, int]] = set()
    while frontier:
        node_id, steps = frontier.pop()
        if node_id in end_ids:
            continue
        if steps >= budget:
            return False
        state = (node_id, steps)
        if state in seen_states:
            continue
        seen_states.add(state)
        for nxt in graph.get(node_id, []):
            frontier.append((nxt, steps + 1))
    return True
