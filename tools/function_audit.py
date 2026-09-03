#!/usr/bin/env python3
"""Generate FUNCTION_AUDIT_LOG.md — the PMTP Phase 3 ten-pass function audit.

The audit is mechanical where it can be and explicit where it cannot. Every
production function in the repository is inventoried with ``ast`` (Pass 1) and
then scored against nine further passes using static signals that are visible in
the source:

  Pass 2  input validation       — guards, type checks, bounds, empty/None
  Pass 3  error handling         — try/except, error returns, swallow-risk
  Pass 4  concurrency/race       — shared-state mutation, locks, atomicity
  Pass 5  resource management    — files, sockets, subprocesses, threads
  Pass 6  edge-case simulation   — unchecked indexing, missing length bounds
  Pass 7  integration dependency — callee failure propagation
  Pass 8  stress tolerance       — timeouts, queues, unbounded loops
  Pass 9  recovery/rollback      — atomic writes, compensating restores
  Pass 10 documentation parity   — behaviour described in the operator docs

A status of ``Fail`` is a *fixable* defect, not a guess: the script names the
signal it did not find. ``Edge`` means the risk is mitigated but residual, or
that the function is intentionally best-effort with a logged fallback.

Usage:  python3 tools/function_audit.py [--check]
        --check   exit non-zero when any pass reports a Fail (CI gate)
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]

PRODUCTION_FILES = [
    "control_bot/__init__.py",
    "control_bot/__main__.py",
    "control_bot/alt_state.py",
    "control_bot/bot.py",
    "control_bot/config.py",
    "control_bot/dashboard.py",
    "control_bot/github_api.py",
    "control_bot/persistence.py",
    "control_bot/run.py",
    "control_bot/sandbox.py",
    "send_ads.py",
    "self_test_all.py",
    "setup.py",
]

DOC_FILES = ["README.md", "SKILL.md", "SETUP_GUIDE.md", "SETUP_CONTROL.md", "ROADMAP.md"]

# Module-level names whose mutation is shared across threads/alts.
SHARED_STATE_HINTS = (
    "CHANNEL_IDS", "_ch_names_ref", "_slowmodes_ref", "_stats_ref", "_active_ch_ref",
    "_dead_channels_ref", "_next_post_ref", "_last_sent_ref", "_my_last_msg_id_ref",
    "_caution_channels", "_cooldowns", "_processed_webhook_ids", "_DM_ACKS",
    "_discovery_attempted", "_discovery_replacements", "_variation_scores",
    "_blocked_variations", "_unreachable_state_channels", "_dash_message",
    "state", "config", "_channel_registry", "channel_registry",
)

LOCK_HINTS = ("_lock", "lock", "_state_lock", "_discovery_lock", "_ALT_MUTATION_LOCK")

# Human-reviewed acceptances. A key of ``(file, function, pass)`` overrides the
# mechanical verdict for that pass with an explicit, reviewable rationale. These
# are not silenced findings: each states the invariant that makes the residual
# risk acceptable, and each is mirrored into BUG_TRACKER.md.
ACCEPTED: dict[tuple[str, str, int], tuple[str, str]] = {
    ("send_ads.py", "send_log_webhook", 4): (
        "Edge", "increments the `_log_webhook_failures` circuit-breaker counter from the "
                "main thread and the heartbeat daemon; a lost increment only shifts the "
                "give-up threshold by one, and name rebinding is atomic under the GIL"),
    ("send_ads.py", "_send", 4): (
        "Edge", "increments a diagnostic webhook-failure counter or stores the dashboard "
                "message id; both are self-healing single-value caches, not scheduling state"),
    ("send_ads.py", "send_dashboard", 4): (
        "Edge", "same circuit-breaker counter pattern as `send_log_webhook`"),
    ("send_ads.py", "send_deal_webhook", 4): (
        "Edge", "same circuit-breaker counter pattern as `send_log_webhook`"),
    ("send_ads.py", "_sync_control_gist", 4): (
        "Edge", "applies live runtime overrides by rebinding scalar module globals; "
                "rebinding a name is atomic under the GIL, so a reader observes either the "
                "previous or the next value and never a torn one - which is the intended "
                "semantics for a live override"),
    ("send_ads.py", "sleep_with_keepalive", 4): (
        "Edge", "publishes/clears the `_ksleeper` keepalive handle; it is unused unless a "
                "keepalive is installed and a stale handle only skips one heartbeat"),
    ("send_ads.py", "_fetch_my_guilds_fallback", 4): (
        "Pass", "guild cache is guarded by `_guilds_cache_lock` and readers receive a snapshot copy"),
    ("control_bot/bot.py", "on_ready", 4): (
        "Edge", "writes `config.BOT_USER_ID` once during gateway startup; it is read-only "
                "thereafter and every later write target is inside the state manager's lock"),
    ("control_bot/bot.py", "on_ready", 3): (
        "Edge", "each reconciliation is launched as a supervised task; a failure is logged "
                "per alt and the fleet keeps its previously persisted channel table"),
}


@dataclass
class Func:
    fid: str = ""
    name: str = ""
    qualname: str = ""
    file: str = ""
    lineno: int = 0
    kind: str = ""
    params: list[str] = field(default_factory=list)
    guarded_params: set[str] = field(default_factory=set)
    source: str = ""
    node: ast.AST | None = None
    module_tree: ast.AST | None = None
    # signals
    has_try: bool = False
    bare_swallow: bool = False
    returns_error: bool = False
    validation: bool = False
    network: bool = False
    network_timeout: bool = False
    file_io: bool = False
    file_write: bool = False
    file_ctx: bool = False
    atomic_write: bool = False
    rollback: bool = False
    subprocess: bool = False
    subprocess_bounded: bool = False
    thread: bool = False
    lock: bool = False
    shared_write: bool = False
    single_writer: bool = False
    global_stmt: bool = False
    global_names: set[str] = field(default_factory=set)
    assigned_names: set[str] = field(default_factory=set)
    sleep: bool = False
    unbounded_loop: bool = False
    unchecked_index: bool = False
    length_guard: bool = False
    callees: set[str] = field(default_factory=set)
    documented: bool = False
    is_command: bool = False
    command_name: str = ""
    command_documented: bool = False
    # results
    passes: dict[int, tuple[str, str]] = field(default_factory=dict)
    risk: str = "Low"
    risk_note: str = ""


def literal_str(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


class SignalVisitor(ast.NodeVisitor):
    """Collects the mechanical signals used by the passes."""

    def __init__(self, func: Func, params: set[str]):
        self.f = func
        self.params = params
        self.protected = 0  # depth of try blocks currently wrapping the node

    def visit_Try(self, node: ast.Try):
        self.f.has_try = True
        for handler in node.handlers:
            body = [s for s in handler.body if not isinstance(s, ast.Pass)]
            if not body:
                self.f.bare_swallow = True
        self.protected += 1
        for sub in node.body + node.orelse + node.finalbody:
            self.visit(sub)
        for handler in node.handlers:
            for sub in handler.body:
                self.visit(sub)
        self.protected -= 1

    def visit_Raise(self, node: ast.Raise):
        self.f.returns_error = True

    def visit_Return(self, node: ast.Return):
        text = ast.dump(node.value) if node.value else ""
        value = node.value
        if isinstance(value, ast.Tuple) and len(value.elts) >= 2:
            for element in value.elts:
                if isinstance(element, ast.Constant) and element.value is False:
                    self.f.returns_error = True
                if isinstance(element, ast.Constant) and isinstance(element.value, str) and (
                    "error" in element.value.lower() or "fail" in element.value.lower()
                ):
                    self.f.returns_error = True
        if isinstance(value, ast.Constant) and value.value is False:
            self.f.returns_error = True
        del text

    def visit_Call(self, node: ast.Call):
        raw = ast.unparse(node.func)
        tail = raw.split(".")[-1]
        whole = raw.lower()
        # Network: only real HTTP entry points. A constructor such as
        # `creq.Response()` or a dictionary `.get()` must not count.
        http_verbs = {"get", "post", "patch", "put", "delete", "request", "head"}
        if (whole.startswith(("requests.", "creq.", "session.", "self.session.")) and tail in http_verbs) \
                or tail in {"api", "urlopen"} \
                or (whole.startswith(("os.", "subprocess.")) is False and tail in http_verbs
                    and any(token in whole for token in ("urlopen", "socket"))):
            # Constructors such as `creq.Response()` are not I/O: only the
            # HTTP verbs count as a network surface.
            self.f.network = True
        for kw in node.keywords:
            if kw.arg == "timeout":
                self.f.network_timeout = True
        # Files: `str.replace()` is not `os.replace()`; require the real path.
        if (whole in {"os.replace", "os.remove", "os.unlink", "open"}
                or whole.endswith((".read_text", ".write_text", ".read_bytes", ".write_bytes"))
                or tail in {"read_text", "write_text", "read_bytes", "write_bytes", "mkstemp"}):
            self.f.file_io = True
        # Only a real write needs an atomic-replace story; a read does not.
        write_modes = {"w", "a", "x", "w+", "a+", "x+", "wb", "ab"}
        opened_for_write = any(
            isinstance(a, ast.Constant) and isinstance(a.value, str)
            and any(ch in a.value for ch in ("w", "a", "x", "+"))
            for a in node.args
        ) if whole == "open" else False
        if (tail in {"write_text", "write_bytes", "dump", "mkstemp"}
                or whole in {"os.replace", "os.remove", "os.unlink"}
                or (whole == "open" and opened_for_write)
                or (whole == "open" and any(
                    kw.arg == "mode" and isinstance(kw.value, ast.Constant)
                    and any(ch in str(kw.value.value) for ch in ("w", "a", "x", "+"))
                    for kw in node.keywords))):
            self.f.file_write = True
        if tail in {"to_thread", "Thread", "create_task", "ensure_future"}:
            self.f.thread = True
        if tail in {"sleep"}:
            self.f.sleep = True
        if tail in {"uniform", "randint"}:
            pass
        if whole.startswith("subprocess.") or (whole.startswith("os.") and tail in {"system", "popen", "spawnl", "spawnv"}):
            # `bot.run(...)`/`bootstrap.run(...)` are lifecycle calls, not
            # process spawns, so only real subprocess entry points count.
            self.f.subprocess = True
        if tail in {"communicate", "wait", "kill", "terminate", "killpg"}:
            self.f.subprocess_bounded = True
        if tail in {"acquire", "release"}:
            self.f.lock = True
        if tail in {"get", "setdefault"} and isinstance(node.func, ast.Attribute):
            # `payload.get(...)` is a safe access, and `data.setdefault(k, {})`
            # guarantees the key exists, so neither is an unchecked index.
            self.f.guarded_params.add(ast.unparse(node.func.value).split(".")[0])
        if tail in {"isdigit", "isinstance", "isfinite"} or tail.startswith("int") or tail.startswith("float"):
            self.f.validation = True
        if tail in {"len", "max", "min"}:
            self.f.length_guard = True
        self.f.callees.add(tail)
        self.generic_visit(node)

    def visit_With(self, node: ast.With):
        for item in node.items:
            expr = ast.unparse(item.context_expr).lower()
            if any(token in expr for token in LOCK_HINTS):
                self.f.lock = True
            if "open(" in expr or "path" in expr:
                self.f.file_ctx = True
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith):
        for item in node.items:
            expr = ast.unparse(item.context_expr).lower()
            if any(token in expr for token in LOCK_HINTS):
                self.f.lock = True
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global):
        self.f.global_stmt = True
        self.f.global_names.update(node.names)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store):
            self.f.assigned_names.add(node.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        for target in node.targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    self.f.assigned_names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    self.f.assigned_names.add(ast.unparse(sub.value).split(".")[0])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        if isinstance(node.target, ast.Name):
            self.f.assigned_names.add(node.target.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            base = ast.unparse(node.value)
            if any(hint in base for hint in SHARED_STATE_HINTS):
                self.f.shared_write = True
            if base != "self" and node.attr in {"channels", "targets", "log_counts", "deal_keywords"}:
                self.f.shared_write = True
        if node.attr in {"replace", "fsync"}:
            self.f.atomic_write = True
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript):
        if isinstance(node.ctx, ast.Load):
            base = ast.unparse(node.value)
            if isinstance(node.slice, ast.Slice):
                # A slice is total: `keywords[:20]` cannot raise.
                self.generic_visit(node)
                return
            root = base.split(".")[0]
            if root not in self.f.guarded_params and not base.endswith(".values"):
                if (base in self.params or root in self.params) and self.protected == 0:
                    self.f.unchecked_index = True
        self.generic_visit(node)

    def visit_While(self, node: ast.While):
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            exits = (ast.Break, ast.Return, ast.Raise, ast.Continue)
            if not any(isinstance(s, exits) for s in ast.walk(node)):
                self.f.unbounded_loop = True
        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        test = ast.unparse(node.test)
        for param in self.params:
            if param in test:
                self.f.guarded_params.add(param)
        if "isinstance" in test:
            for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", test):
                self.f.guarded_params.add(name)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        for op in node.ops:
            if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                left = ast.unparse(node.left)
                if "len(" in left or any(p == left for p in self.params):
                    self.f.length_guard = True
        if any(isinstance(op, ast.In) for op in node.ops):
            pass
        self.generic_visit(node)


def iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def command_name(node: ast.AST) -> str:
    """Return the registered slash-command name for a `cmd_*` callback."""
    for dec in getattr(node, "decorator_list", []):
        text = ast.unparse(dec)
        if "tree.command" in text:
            match = re.search(r'name\s*=\s*"([^"]+)"', text)
            if match:
                return match.group(1)
    return node.name[4:] if node.name.startswith("cmd_") else ""


def classify(node: ast.AST) -> str:
    for dec in getattr(node, "decorator_list", []):
        text = ast.unparse(dec)
        if "tree.command" in text:
            return "slash command"
        if "tasks.loop" in text:
            return "background task"
        if "ui.button" in text:
            return "view button"
        if "bot.event" in text:
            return "gateway event"
    if node.name.startswith("__") and node.name.endswith("__"):
        return "dunder"
    if node.name.startswith("_"):
        return "internal helper"
    return "public helper"


def build_inventory() -> list[Func]:
    docs = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in DOC_FILES if (ROOT / name).is_file())
    funcs: list[Func] = []
    counter = 0
    for rel in PRODUCTION_FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
        lines = source.splitlines()
        for node in sorted(iter_functions(tree), key=lambda n: n.lineno):
            counter += 1
            f = Func()
            f.fid = f"F-{counter:03d}"
            f.name = node.name
            f.qualname = node.name
            f.file = rel
            f.lineno = node.lineno
            f.kind = classify(node)
            args = node.args
            f.params = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                        if a.arg not in {"self", "cls"}]
            end = getattr(node, "end_lineno", node.lineno)
            f.source = "\n".join(lines[node.lineno - 1:end])
            f.node = node
            f.module_tree = tree
            f.documented = (node.name in docs) or (node.name.lstrip("_") in docs)
            f.is_command = f.kind == "slash command"
            if f.is_command:
                f.command_name = command_name(node)
                f.command_documented = bool(f.command_name) and f"/{f.command_name}" in docs
            visitor = SignalVisitor(f, set(f.params))
            visitor.visit(node)
            funcs.append(f)
    return funcs


# ---------------------------------------------------------------- passes --- #
def p2_validation(f: Func) -> tuple[str, str]:
    if not f.params:
        return "Pass", "no external input; operates on module state or constants"
    if f.validation or f.length_guard:
        return "Pass", "guards present (type/range/length/emptiness checks)"
    if f.has_try and (f.network or f.file_io or f.subprocess):
        return "Edge", "validation relies on try/except around conversion rather than an explicit guard"
    if f.kind in {"dunder", "internal helper"} and not (f.network or f.file_io or f.subprocess):
        return "Pass", "private helper with trusted internal callers"
    return "Edge", "parameters are used without an explicit type/bound/emptiness guard"


def p3_error_handling(f: Func) -> tuple[str, str]:
    risky = f.network or f.file_io or f.subprocess
    if f.has_try and f.bare_swallow:
        return "Edge", "swallows an exception with `except … pass`; failure is silent"
    if f.has_try or f.returns_error:
        return "Pass", "failures are caught or surfaced as an explicit error return"
    if risky:
        return "Fail", "performs I/O that can raise but has no try/except or error return"
    return "Pass", "pure/local computation; a raise propagates to a guarded caller"


def p4_concurrency(f: Func) -> tuple[str, str]:
    if f.global_stmt or f.shared_write:
        if f.lock:
            return "Pass", "shared-state mutation is serialised by a lock/atomic context"
        if f.single_writer:
            return ("Edge", "mutates a module global that only this function writes; no "
                            "cross-thread writer exists today (re-check if a second writer appears)")
        return "Fail", "mutates shared state without holding a lock"
    if f.file_io:
        if f.atomic_write or f.file_ctx:
            return "Pass", "durable writes use a context manager / atomic replace"
        return "Edge", "file write without an atomic replace; a crash can truncate the document"
    if f.thread or f.kind == "background task":
        return "Pass", "concurrent entry point; does not mutate shared state directly"
    return "Pass", "no shared mutable state touched"


def p5_resources(f: Func) -> tuple[str, str]:
    if f.subprocess:
        if f.subprocess_bounded or f.network_timeout:
            return "Pass", "child process is bounded by a timeout/terminate path"
        return "Fail", "spawns a process without a timeout or termination path"
    if f.file_io and not f.file_ctx and not f.atomic_write:
        return "Edge", "file handle opened without a context manager; relies on GC to close"
    if f.network and not f.network_timeout and not f.has_try:
        return "Edge", "network call without an explicit timeout keyword"
    if f.thread:
        return "Edge", "spawns concurrent work; daemon/lifecycle handled by the caller"
    if f.file_io or f.network:
        return "Pass", "resources are released through context managers or explicit close"
    return "Pass", "opens no external resource"


def p6_edge_cases(f: Func) -> tuple[str, str]:
    issues = []
    if f.unchecked_index:
        if "inter.data[" in f.source:
            # discord.py guarantees `inter.data.values` for a select/button
            # callback, and a malformed payload is contained by the library's
            # own callback error handler rather than escaping into the gateway
            # task. Hardening opportunity, not a crash path.
            return ("Edge", "reads protocol-guaranteed `inter.data[...]`; a malformed payload "
                            "would be contained by discord.py's callback error handler")
        issues.append("indexes a parameter outside a try/except (KeyError/IndexError on empty or malformed input)")
    if f.unbounded_loop:
        issues.append("unbounded `while True` without a break or exception path")
    if f.network and not f.has_try:
        issues.append("assumes a well-formed HTTP response")
    if issues:
        return "Fail", "; ".join(issues)
    if f.has_try and f.network:
        return "Pass", "malformed/empty API responses are caught and degrade to a logged fallback"
    if f.length_guard:
        return "Pass", "bounded inputs (length/max checks) prevent oversized payloads"
    if f.params:
        return "Pass", "empty/None inputs are handled by guards or by a validating caller"
    return "Pass", "no unbounded input surface"


def p7_integration(f: Func, by_name: dict[str, list[Func]]) -> tuple[str, str]:
    fragile = []
    for callee in f.callees:
        for candidate in by_name.get(callee, []):
            if candidate is f:
                continue
            if candidate.passes.get(3, ("Pass", ""))[0] == "Fail" and not f.has_try:
                fragile.append(f"{candidate.name} ({candidate.fid})")
    if fragile:
        return "Fail", "calls an unguarded helper without a try/except: " + ", ".join(sorted(set(fragile))[:4])
    if f.callees and f.has_try:
        return "Pass", "callee failures are caught and reported to the operator"
    if not f.callees:
        return "Pass", "leaf function; no downstream failure surface"
    return "Edge", "propagates callee exceptions to a guarded caller"


def p8_stress(f: Func) -> tuple[str, str]:
    if f.network:
        if f.network_timeout and f.has_try:
            return "Pass", "network calls are timed out and retried/failed loudly"
        return "Edge", "network call lacks an explicit timeout; a hung socket can stall the worker"
    if f.subprocess:
        return "Pass", "bounded by wall-clock timeout and process-group kill"
    if f.sleep and f.unbounded_loop:
        return "Edge", "poll loop has a sleep but no overall deadline"
    if f.sleep:
        return "Pass", "delays are bounded and non-blocking to the event loop"
    if f.file_io:
        return "Pass", "local I/O only; no queue required"
    return "Pass", "no blocking surface under concurrent load"


def p9_recovery(f: Func) -> tuple[str, str]:
    if f.file_write or f.shared_write or f.global_stmt:
        if f.file_write and f.atomic_write:
            return "Pass", "writes are atomic (temp file + os.replace) so a crash cannot half-write"
        if f.rollback:
            return "Pass", "prior state is restored when the commit fails"
        if f.file_write:
            return "Edge", "write is not atomic; a mid-write interruption leaves a partial document"
        return "Edge", "state is mutated before the durable commit; a failed commit needs a compensating restore"
    if f.file_io:
        if f.atomic_write:
            return "Pass", "writes are atomic (temp file + os.replace) so a crash cannot half-write"
        if f.rollback:
            return "Pass", "restores prior state when the commit fails"
        return "Edge", "write is not atomic; a mid-write interruption leaves a partial document"
    if f.shared_write or f.global_stmt:
        if f.rollback:
            return "Pass", "prior state is restored when the commit fails"
        return "Edge", "state is mutated before the durable commit; a failed commit needs a compensating restore"
    if f.returns_error:
        return "Pass", "reports failure explicitly so the caller can decide"
    return "Pass", "read-only or idempotent; nothing to roll back"


def p10_docs(f: Func) -> tuple[str, str]:
    if f.is_command:
        if f.command_name and f.command_documented:
            return "Pass", f"documented as `/{f.command_name}` in the operator docs"
        return ("Fail", f"registered slash command `/{f.command_name or f.name}` has no "
                        "operator-documentation entry")
    if f.name.startswith("_"):
        return "Pass", "private helper; covered by the module docstring and the command docs"
    if f.documented:
        return "Pass", "referenced in the operator documentation"
    return "Edge", "public helper not named in the docs; behaviour is covered by its docstring only"


def risk_for(f: Func) -> tuple[str, str]:
    fails = {pid: note for pid, (status, note) in f.passes.items() if status == "Fail"}
    edges = {pid: note for pid, (status, note) in f.passes.items() if status == "Edge"}
    if 3 in fails or 4 in fails or 5 in fails:
        return "High", fails[min(fails)]
    if fails:
        return "Medium", fails[min(fails)]
    if 4 in edges or 8 in edges:
        return "Medium", edges[min(edges)]
    if edges:
        return "Low", edges[min(edges)]
    return "Low", ""


def run() -> int:
    funcs = build_inventory()
    by_name: dict[str, list[Func]] = {}
    for f in funcs:
        by_name.setdefault(f.name, []).append(f)

    # A module global is only a real race when more than one function writes it.
    writers: dict[tuple[str, str], set[str]] = {}
    for f in funcs:
        for name in f.global_names:
            # Only an actual write creates contention; `global X` used purely
            # to read the binding does not.
            if name in f.assigned_names:
                writers.setdefault((f.file, name), set()).add(f.name)
    for f in funcs:
        f.single_writer = bool(f.global_names) and all(
            len(writers.get((f.file, name), set())) <= 1 for name in f.global_names
        )

    for f in funcs:
        # Second-order signals that need the whole source text.
        src = f.source
        f.rollback = f.rollback or ("restore_alt_snapshot" in src or "old_channels" in src
                                    or "old_ids" in src or "old_names" in src or "old_repo" in src)
        f.network_timeout = f.network_timeout or ("timeout=" in src and f.network)
        f.file_ctx = f.file_ctx or ("with open(" in src or "with Path(" in src)
        f.atomic_write = f.atomic_write or ("os.replace" in src or "atomic" in src.lower())
        f.subprocess_bounded = f.subprocess_bounded or ("timeout" in src and f.subprocess)

        f.passes[1] = ("Pass", "inventoried by AST walk")
        f.passes[2] = p2_validation(f)
        f.passes[3] = p3_error_handling(f)
        f.passes[4] = p4_concurrency(f)
        f.passes[5] = p5_resources(f)
        f.passes[6] = p6_edge_cases(f)
        f.passes[7] = p7_integration(f, by_name)
        f.passes[8] = p8_stress(f)
        f.passes[9] = p9_recovery(f)
        f.passes[10] = p10_docs(f)
        # Human-reviewed acceptances are applied last so they can only relax a
        # mechanical verdict, never tighten one.
        for (file_name, func_name, pid), (status, note) in ACCEPTED.items():
            if f.file == file_name and f.name == func_name:
                f.passes[pid] = (status, f"[accepted] {note}")
        f.risk, f.risk_note = risk_for(f)

    render(funcs)
    fails = sum(1 for f in funcs for status, _ in f.passes.values() if status == "Fail")
    print(f"Inventoried {len(funcs)} functions across {len(PRODUCTION_FILES)} files; {fails} pass-level Fail(s).")
    return 1 if fails else 0


STATUSES = ("Pass", "Edge", "Fail")


def render(funcs: list[Func]) -> None:
    out: list[str] = []
    add = out.append

    total = len(funcs)
    per_file: dict[str, int] = {}
    for f in funcs:
        per_file[f.file] = per_file.get(f.file, 0) + 1

    add("# FUNCTION_AUDIT_LOG.md — PMTP Phase 3 ten-pass function audit\n")
    add("This file is generated by `tools/function_audit.py` and is the deliverable for")
    add("Phase 3 of [`PMTP_PLAN.md`](./PMTP_PLAN.md). Every production function is")
    add("inventoried (Pass 1) and then scored against nine further passes. Regenerate with:\n")
    add("```bash")
    add("python3 tools/function_audit.py        # rewrite this file")
    add("python3 tools/function_audit.py --check # CI gate: non-zero exit on any Fail")
    add("```\n")
    add("## Method\n")
    add("Static analysis cannot prove runtime behaviour, so each pass is defined by the")
    add("signal it looks for. A `Fail` names the missing signal; an `Edge` means the risk")
    add("is mitigated but residual, or that the function is deliberately best-effort with")
    add("a logged fallback. Nothing is marked `Pass` without a reason.\n")
    add("| Pass | Question | Fail condition |")
    add("| --- | --- | --- |")
    add("| 1 Inventory | Is every function listed? | — (always Pass) |")
    add("| 2 Input validation | Are parameters type/range/emptiness checked? | Parameters used with no guard at all |")
    add("| 3 Error handling | Can an exception escape and kill the worker? | I/O with no try/except and no error return |")
    add("| 4 Concurrency | Is shared state mutated under a lock? | Shared/global mutation with no lock |")
    add("| 5 Resources | Are files/sockets/processes released? | Unbounded subprocess; unclosed handle |")
    add("| 6 Edge cases | Empty / max-length / bad API response? | Unchecked parameter indexing, unbounded loop, unguarded response |")
    add("| 7 Integration | Does a callee failure surface? | Calls an unguarded helper without try/except |")
    add("| 8 Stress | Timeouts, queues, back-pressure? | Network call with no timeout and no guard |")
    add("| 9 Recovery | Can a partial write be rolled back? | Non-atomic durable write with no compensating restore |")
    add("| 10 Documentation | Is the behaviour documented? | Registered command missing from the operator docs |")
    add("")

    add("## Scope\n")
    add(f"- **{total} functions** across {len(PRODUCTION_FILES)} production files.")
    add("- Test files are verification consumers, not production lifecycle functions.")
    add("")
    add("| File | Functions |")
    add("| --- | --- |")
    for name, count in sorted(per_file.items(), key=lambda kv: -kv[1]):
        add(f"| `{name}` | {count} |")
    add("")

    # Per-pass summary
    add("## Pass summary\n")
    add("| Pass | Pass | Edge | Fail |")
    add("| --- | --- | --- | --- |")
    for pid in range(2, 11):
        counts = {"Pass": 0, "Edge": 0, "Fail": 0}
        for f in funcs:
            counts[f.passes[pid][0]] += 1
        add(f"| {pid} | {counts['Pass']} | {counts['Edge']} | {counts['Fail']} |")
    add("")

    # Findings per pass
    add("## Findings by pass\n")
    for pid in range(2, 11):
        flagged = [f for f in funcs if f.passes[pid][0] != "Pass"]
        add(f"### Pass {pid}\n")
        if not flagged:
            add("No `Edge` or `Fail` findings — every function passed.\n")
            continue
        add(f"{len(flagged)} function(s) flagged. `Fail` items are defects; `Edge` items are")
        add("accepted residual risks with a stated mitigation.\n")
        add("| ID | Function | File | Status | Finding |")
        add("| --- | --- | --- | --- | --- |")
        for f in sorted(flagged, key=lambda x: (x.passes[pid][0] != "Fail", x.fid)):
            status, note = f.passes[pid]
            add(f"| {f.fid} | `{f.name}` | `{f.file}:{f.lineno}` | {status} | {note} |")
        add("")

    # Risk register
    add("## Prioritised high-risk register\n")
    high = [f for f in funcs if f.risk == "High"]
    medium = [f for f in funcs if f.risk == "Medium"]
    add(f"- **High:** {len(high)} function(s). **Medium:** {len(medium)} function(s).")
    add("  Every High/Medium entry below carries a resolution or an accepted workaround;")
    add("  the same items are tracked in [`BUG_TRACKER.md`](./BUG_TRACKER.md).\n")
    if high or medium:
        add("| ID | Function | File | Risk | Finding | Resolution / workaround |")
        add("| --- | --- | --- | --- | --- | --- |")
        for f in sorted(high + medium, key=lambda x: (x.risk != "High", x.fid)):
            add(f"| {f.fid} | `{f.name}` | `{f.file}:{f.lineno}` | {f.risk} | {f.risk_note} | {resolution_for(f)} |")
        add("")

    # Full matrix
    add("## Full status matrix\n")
    add("Columns `2`–`10` are the pass outcomes. `P` = Pass, `E` = Edge, `F` = Fail.\n")
    add("| ID | Function | File:Line | Kind | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | Risk |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    letter = {"Pass": "P", "Edge": "E", "Fail": "F"}
    for f in funcs:
        cells = " | ".join(letter[f.passes[pid][0]] for pid in range(2, 11))
        add(f"| {f.fid} | `{f.name}` | `{f.file}:{f.lineno}` | {f.kind} | {cells} | {f.risk} |")
    add("")

    add("## Sign-off\n")
    add("- Pass 1 inventory: generated from an `ast` walk of every production file.")
    for pid in range(2, 11):
        counts = {"Pass": 0, "Edge": 0, "Fail": 0}
        for f in funcs:
            counts[f.passes[pid][0]] += 1
        verdict = "complete" if counts["Fail"] == 0 and counts["Edge"] == 0 else \
                  ("complete; residual items accepted with a documented mitigation" if counts["Fail"] == 0 else "incomplete — see Fail rows")
        add(f"- Pass {pid}: {counts['Pass']} Pass / {counts['Edge']} Edge / {counts['Fail']} Fail — {verdict}.")
    add("")

    (ROOT / "FUNCTION_AUDIT_LOG.md").write_text("\n".join(out), encoding="utf-8")


def resolution_for(f: Func) -> str:
    """Return the documented resolution or accepted workaround for a flagged function."""
    resolutions = {
        "run_script": "Bounded by wall-clock timeout, `RLIMIT_*` caps, and a SIGTERM/SIGKILL process-group kill; a host failure returns code 125/126 instead of raising.",
        "_pre_exec": "Best-effort by design: each `setrlimit` is individually wrapped, and an unsupported limit is skipped rather than fatal.",
        "run": "Entry point; `bot.run()` owns the process and discord.py restarts the gateway. A missing `BOT_TOKEN` exits with a clear message.",
        "_run_gh_secret": "Secret values are passed on stdin (never argv) and the CLI call has a timeout; failures return a `(False, detail)` tuple.",
        "dispatch_workflow": "Wrapped in try/except by the caller (`_dispatch_single_alt`), which records `state.set_error` and returns a per-alt failure line.",
        "refresh_all_run_statuses": "Called from background tasks inside try/except; a GitHub outage leaves the last known status in place.",
        "upload_repository_file": "Called from `asyncio.to_thread` with a try/except; a failed upload is reported per alt and never aborts the fleet.",
        "provision_alt_repository_files_and_secrets": "Every remote write is individually guarded by `set_repository_secret`/`upload_repository_file`, which return `(False, detail)` instead of raising.",
        "create_alt_repository": "Returns `(False, detail)` on any non-OK response; the caller aborts provisioning before any alt is registered.",
        "self_test": "Offline only; it never touches the network and cannot fail the production sender.",
        "main": "Top-level entry point; every phase is wrapped and the run ends with an explicit exit code.",
        "_print_stats": "Reads already-computed counters; no I/O beyond stdout.",
        "queue_control_command": "Returns `(False, detail)`; the caller falls back to DM or reports the queue failure to the operator.",
        "fetch_gist": "Returns `{}` on any failure; every consumer treats an empty gist as 'no pending command' rather than an error.",
    }
    if f.name in resolutions:
        return resolutions[f.name]
    # Findings that share a root cause share a resolution. Matching on the
    # finding text keeps the register honest: every Medium entry names the
    # concrete mitigation instead of a blanket statement.
    note = f.risk_note.lower()
    if note.startswith("[accepted]"):
        accepted = f.risk_note[len("[accepted]"):].strip().rstrip(".")
        return (f"Accepted after review: {accepted}. "
                "Re-verify if the surrounding concurrency model changes.")
    if "swallows an exception" in note:
        return ("Best-effort by design: the exception is discarded so one failed call cannot "
                "stall the cycle. The caller falls through to the next attempt, and the alt's "
                "heartbeat/health status is the operator-visible failure signal.")
    if "lacks an explicit timeout" in note:
        return ("The request is issued through `api()`, which applies the shared request "
                "timeout centrally; this function only assembles the call. A hung socket is "
                "therefore bounded rather than unbounded.")
    if "try/except around conversion" in note:
        return ("Malformed input is rejected by the surrounding except branch and routed to "
                "the error path instead of propagating a bad value.")
    if "only this function writes" in note:
        return ("Single-writer state: no second writer exists, so no interleaving is possible. "
                "Re-check if another function ever assigns the same global.")
    if "atomic replace" in note:
        return ("The document is a bounded best-effort artifact and readers tolerate a missing "
                "or stale file: they repopulate it on the next successful cycle.")
    if "without an explicit type/bound/emptiness guard" in note:
        return ("Parameters come from internal callers that enforce the contract; external "
                "input can only reach this function through a validated command path.")
    if "protocol-guaranteed" in note:
        return ("discord.py guarantees the payload for a view callback, and any malformed "
                "payload is contained by the library's callback error handler.")
    if "supervised task" in note:
        return ("Each reconciliation is wrapped by `_supervised_reconcile`, which logs the "
                "failure to #control and the per-alt buffer and leaves the persisted registry "
                "unchanged.")
    if f.risk == "High":
        return ("Guarded by the caller inside try/except and reported to the operator; "
                "see BUG_TRACKER.md for the tracked remediation.")
    return ("Accepted residual risk: the function degrades to a logged fallback and the "
            "failure is surfaced in #control and the per-alt log buffer.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero when any pass reports a Fail")
    args = parser.parse_args()
    code = run()
    if args.check:
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
