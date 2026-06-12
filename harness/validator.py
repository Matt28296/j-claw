from __future__ import annotations
from graphlib import TopologicalSorter, CycleError
import jsonschema


class OrchestratorOutputError(Exception):
    pass


_TASK_SCHEMA = {
    "type": "object",
    "required": ["id", "type", "objective", "files", "dependencies",
                 "priority", "acceptance_criteria", "verification"],
    "properties": {
        "id":                  {"type": "string", "pattern": r"^task-\d+$"},
        "type":                {"type": "string"},
        "objective":           {"type": "string"},
        "files":               {"type": "array", "items": {"type": "string"}},
        "dependencies":        {"type": "array", "items": {"type": "string"}},
        "priority":            {"type": "string", "enum": ["low", "medium", "high"]},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "verification":        {"type": "string",
                                "enum": ["lint", "unit_test", "build", "smoke", "manual", "none",
                                         "ffprobe", "frame_integrity", "sync_check",
                                         "security", "lighthouse"]},
    },
}

_FORMAT1_SCHEMA = {
    "type": "object",
    "required": ["project_type", "complexity", "goal", "features",
                 "constraints", "architecture", "modules"],
    "properties": {
        "project_type": {"type": "string", "enum": ["web", "app", "game", "film"]},
        "complexity":   {"type": "string", "enum": ["low", "medium"]},
        "goal":         {"type": "string"},
        "features":     {"type": "array", "items": {"type": "string"}},
        "constraints":  {"type": "array", "items": {"type": "string"}},
        "architecture": {
            "type": "object",
            "required": ["frontend", "backend", "database", "deployment"],
        },
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "responsibility"],
            },
        },
    },
}

_FORMAT2_SCHEMA = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {"type": "array", "items": _TASK_SCHEMA, "maxItems": 100},
    },
}

_FORMAT3_SCHEMA = {
    "type": "object",
    "required": ["refinement_target_task_id", "reason_for_refinement", "action", "updated_tasks"],
    "properties": {
        "refinement_target_task_id": {"type": "string"},
        "reason_for_refinement":     {"type": "string"},
        "action":                    {"type": "string", "enum": ["modify", "split", "deprecate"]},
        "updated_tasks":             {"type": "array", "items": _TASK_SCHEMA},
    },
}

_FORMAT4_SCHEMA = {
    "type": "object",
    "required": ["review_result", "summary", "followup_tasks"],
    "properties": {
        "review_result":  {"type": "string", "enum": ["pass", "needs_followup"]},
        "summary":        {"type": "string"},
        "followup_tasks": {"type": "array", "items": _TASK_SCHEMA},
    },
}

_FORMAT5_SCHEMA = {
    "type": "object",
    "required": ["oversize", "reason", "sub_projects"],
    "properties": {
        "oversize": {"type": "boolean", "const": True},
        "reason":   {"type": "string"},
        "sub_projects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "goal", "depends_on"],
                "properties": {
                    "name":       {"type": "string"},
                    "goal":       {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_STATE_SCHEMA = {
    "INIT":            _FORMAT1_SCHEMA,
    "SPEC_REVISION":   _FORMAT1_SCHEMA,
    "SPEC_ACCEPTED":   _FORMAT2_SCHEMA,
    "EXECUTION_ERROR": _FORMAT3_SCHEMA,
    "PROJECT_REVIEW":  _FORMAT4_SCHEMA,
    "REVIEW_FAILED":   _FORMAT4_SCHEMA,  # same response shape as PROJECT_REVIEW
}


# ── public ──────────────────────────────────────────────────────────────────

def validate_response(state: str, data: dict) -> None:
    """Validate an orchestrator response.  Raises OrchestratorOutputError on failure."""
    if data.get("oversize") is True:
        _check(data, _FORMAT5_SCHEMA, "FORMAT 5")
        _validate_format5_subproject_dag(data)
        return

    schema = _STATE_SCHEMA.get(state)
    if schema is None:
        raise OrchestratorOutputError(f"Unknown system_state: {state!r}")

    _check(data, schema, state)

    if state == "SPEC_ACCEPTED":
        validate_dag(data["tasks"], existing=[])
    elif state == "EXECUTION_ERROR":
        _validate_format3_rules(data)
    elif state in ("PROJECT_REVIEW", "REVIEW_FAILED") and data["review_result"] == "needs_followup":
        for t in data["followup_tasks"]:
            _check(t, _TASK_SCHEMA, "followup task")


def validate_dag(new_tasks: list[dict], existing: list[dict]) -> None:
    """
    Full DAG integrity check.
    new_tasks   — tasks being added now
    existing    — tasks already in the active DAG (may be empty)
    """
    all_tasks = existing + new_tasks
    all_ids = [t["id"] for t in all_tasks]

    if len(all_ids) != len(set(all_ids)):
        raise OrchestratorOutputError("Duplicate task ids in DAG")

    id_set = set(all_ids)
    for t in new_tasks:
        for dep in t["dependencies"]:
            if dep not in id_set:
                raise OrchestratorOutputError(
                    f"Task {t['id']} references unknown dependency {dep!r}"
                )

    graph = {t["id"]: set(t["dependencies"]) for t in all_tasks}
    try:
        topo_order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise OrchestratorOutputError(f"DAG contains a cycle: {exc}") from exc

    # No concurrent writes to the same file without a dependency edge
    task_map = {t["id"]: t for t in all_tasks}
    file_first_writer: dict[str, str] = {}

    for tid in topo_order:
        if tid not in task_map:
            continue
        task = task_map[tid]
        for f in task.get("files", []):
            if f in file_first_writer:
                other = file_first_writer[f]
                if not (_is_ancestor(other, tid, graph) or _is_ancestor(tid, other, graph)):
                    raise OrchestratorOutputError(
                        f"Tasks {tid!r} and {other!r} both write '{f}' with no dependency between them"
                    )
            else:
                file_first_writer[f] = tid


# ── private helpers ──────────────────────────────────────────────────────────

def _check(data: dict, schema: dict, label: str) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise OrchestratorOutputError(f"{label} schema error: {exc.message}") from exc


def _is_ancestor(ancestor_id: str, descendant_id: str, graph: dict[str, set]) -> bool:
    """Return True if ancestor_id is a (transitive) dependency of descendant_id."""
    visited: set[str] = set()
    stack = list(graph.get(descendant_id, []))
    while stack:
        node = stack.pop()
        if node == ancestor_id:
            return True
        if node not in visited:
            visited.add(node)
            stack.extend(graph.get(node, []))
    return False


def _validate_format3_rules(data: dict) -> None:
    action = data["action"]
    tasks = data["updated_tasks"]
    target = data["refinement_target_task_id"]

    if action == "deprecate" and tasks:
        raise OrchestratorOutputError("FORMAT 3 deprecate must have empty updated_tasks")
    if action == "modify":
        if len(tasks) != 1:
            raise OrchestratorOutputError("FORMAT 3 modify must contain exactly one task")
        if tasks[0]["id"] != target:
            raise OrchestratorOutputError(
                f"FORMAT 3 modify task id {tasks[0]['id']!r} must match target {target!r}"
            )
    if action == "split" and not tasks:
        raise OrchestratorOutputError("FORMAT 3 split must have at least one task")
    if action == "split" and tasks[0]["id"] != target:
        raise OrchestratorOutputError(
            f"FORMAT 3 split first task id {tasks[0]['id']!r} must match target {target!r}"
        )


def _validate_format5_subproject_dag(data: dict) -> None:
    names = {sp["name"] for sp in data["sub_projects"]}
    for sp in data["sub_projects"]:
        for dep in sp.get("depends_on", []):
            if dep not in names:
                raise OrchestratorOutputError(
                    f"Sub-project {sp['name']!r} depends on unknown {dep!r}"
                )
    graph = {sp["name"]: set(sp.get("depends_on", [])) for sp in data["sub_projects"]}
    try:
        list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise OrchestratorOutputError(f"FORMAT 5 sub-project graph has a cycle: {exc}") from exc
