"""Opt-in AST allowlist for scripts sent through `execute_blender_code`.

`execute_code` on the Blender socket is arbitrary code execution inside the
user's Blender process — that is the product feature, so by default nothing is
validated. Setting BLENDER_MCP_SAFE_MODE=1 turns on this validator in the MCP
server, so a script authored by the model must clear it before a byte crosses
the socket. The threat it addresses is prompt injection: third-party text
(asset names and descriptions from Poly Haven, Sketchfab, Hyper3D) flows into
the model's context, and injected instructions could steer the model into
writing hostile code that a user approves without reading.

Scope is the honest limitation: the addon's socket accepts a raw
`execute_code` from any local process, so this guard covers the MCP path only.
It is a guard on what the model can be talked into, not a sandbox around
Blender.

This module is adapted from blender-mcp-desktop's `security/sandbox.py` and
keeps its validator mechanics unchanged — deny-by-default node walk, resolved
dotted-path rules, the unrooted-navigation backstop, shadowing rejection. Only
the policy tables differ, because there `execute_code` is a fallback hatch
beside a full intent system while here it is the primary tool:

ALLOWED here (blocked in the desktop policy):
  * rendering (`bpy.ops.render.render` / `opengl`) and render settings
  * saving/opening .blend files (`save_mainfile`, `open_mainfile`, recover/
    revert) — scene management is the user's business here
  * every import/export operator, plus image/sound/font/movieclip/cachefile
    operators and datablock `.load()` — filesystem access *through bpy* is a
    core use of this project
  * `bpy.path` helpers and assigning `.filepath` / `.mode`

STILL BLOCKED (security properties, not app-architecture decisions):
  * interpreter escapes: eval/exec/compile/__import__/open, the dunder ladder
    (`__class__` → `__subclasses__` → `__globals__`), computed getattr names
  * every module with process/filesystem/network primitives (os, sys,
    subprocess, socket, ...) — only bpy, bmesh, mathutils, and pure-python
    stdlib modules import
  * persistence: `bpy.app.handlers`, `bpy.app.timers`, drivers and driver
    expressions, `register_class`, `bpy.props`, RNA-type assignment
  * code-execution operators: `bpy.ops.script.*`, `bpy.ops.text.*`,
    `bpy.ops.preferences.*` (addon install), `bpy.ops.extensions.*`
    (extension install, the 4.2+ successor), `bpy.ops.console.*`
  * script search paths: `preferences.filepaths.script_directories` (3.6+)
    and the pre-3.6 `script_directory`, plus `use_scripts_auto_execute`
  * external .blend datablock loading (`wm.append`/`wm.link`/`lib_relocate`/
    `lib_reload`) — a hostile .blend carries drivers that run on load
  * `bpy.data.texts` / `.scripts` / `.libraries`, `bpy.utils.execfile` and
    friends, `save_homefile`, `url_open` / `path_open`, `quit_blender`

The attacker is assumed to have read this file, so the policy is structural
rather than pattern-matching: anything that could produce an unbounded name or
attribute at runtime is rejected even when a specific instance would have been
harmless.
"""

from __future__ import annotations

import ast
import os
from typing import Final

__all__ = [
    "SAFE_MODE_ENV",
    "safe_mode_enabled",
    "SandboxViolation",
    "validate_code",
    "is_safe",
    "ALLOWED_MODULES",
    "ALLOWED_BUILTINS",
]

SAFE_MODE_ENV: Final[str] = "BLENDER_MCP_SAFE_MODE"


def safe_mode_enabled() -> bool:
    """True when the user has opted in via BLENDER_MCP_SAFE_MODE."""
    return os.environ.get(SAFE_MODE_ENV, "").strip().lower() in ("true", "1", "yes", "on")


class SandboxViolation(Exception):
    """Raised when a script contains a construct safe mode will not allow.

    Carries the source line so the rejection names *where* the script went
    wrong; the model gets the message back and can retry with a corrected
    script.
    """

    def __init__(self, message: str, node_line: int | None = None) -> None:
        self.node_line = node_line
        self.message = message
        super().__init__(f"line {node_line}: {message}" if node_line else message)


# --- module policy --------------------------------------------------------

#: The only importable modules. None of them exposes process, filesystem, or
#: network primitives. `json` is included deliberately: it can parse and
#: serialize, but it cannot open a file on its own.
ALLOWED_MODULES: Final[frozenset[str]] = frozenset(
    {
        "bpy",
        "bmesh",
        "mathutils",
        "math",
        "cmath",
        "random",
        "colorsys",
        "json",
        "itertools",
        "functools",
        "collections",
        "statistics",
        "string",
        "re",
        "enum",
        "dataclasses",
        "typing",
        "decimal",
        "fractions",
        "textwrap",
        "unicodedata",
        "uuid",
        "copy",
        "heapq",
        "bisect",
        "array",
    }
)
# NOTE: numpy is deliberately absent despite shipping inside Blender.
# `numpy.load` on a pickled .npy is arbitrary deserialization (code execution),
# and allowing the root package would mean enumerating its I/O surface forever.

_DENIED_SUBMODULES: Final[frozenset[str]] = frozenset(
    {
        "bpy.utils.previews",  # loads files from disk into the UI layer
        "random.SystemRandom",  # not a module, but blocked as a from-import name
        "collections.abc",  # harmless, but keeps the from-import surface tight
    }
)

#: Names that may never be imported *from* an otherwise-allowed module. These
#: are the escape hatches a permitted package still exposes.
_DENIED_IMPORT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "system",
        "popen",
        "SystemRandom",
        "previews",
        "path",
        "environ",
        "exit",
        "argv",
        "modules",
        "builtins",
        "__builtins__",
        "__import__",
        "__loader__",
        "__spec__",
        "reload",
        "import_module",
        "find_spec",
        "util",
    }
)


# --- builtin policy -------------------------------------------------------

#: Builtins a script may call. Anything absent is rejected, which is what makes
#: this deny-by-default: a new dangerous builtin in a future Python is blocked
#: automatically because it was never added here.
ALLOWED_BUILTINS: Final[frozenset[str]] = frozenset(
    {
        # constructors / conversions
        "bool", "int", "float", "complex", "str", "bytes", "bytearray",
        "list", "tuple", "set", "frozenset", "dict", "slice",
        # numeric
        "abs", "round", "min", "max", "sum", "pow", "divmod",
        "hex", "oct", "bin", "ord", "chr",
        # iteration
        "len", "range", "enumerate", "zip", "map", "filter", "reversed",
        "sorted", "all", "any", "iter", "next",
        # inspection that cannot be turned into a capability
        "isinstance", "issubclass", "callable", "repr", "format", "hash", "id",
        "print", "type",
        # exceptions a script may legitimately raise or catch
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "RuntimeError", "AttributeError", "ZeroDivisionError", "StopIteration",
        "NotImplementedError", "ArithmeticError", "OverflowError",
        "LookupError", "AssertionError", "FloatingPointError",
        # constants
        "True", "False", "None", "NotImplemented", "Ellipsis",
    }
)

#: Callables that are always fatal. Listed explicitly so the violation message
#: names the actual hazard.
_FORBIDDEN_CALLABLES: Final[dict[str, str]] = {
    "eval": "eval() executes arbitrary expressions",
    "exec": "exec() executes arbitrary code",
    "compile": "compile() produces executable code objects",
    "__import__": "__import__() bypasses the import allowlist",
    "open": "open() grants raw filesystem access; use bpy operators for file work",
    "input": "input() blocks Blender's main thread on stdin",
    "breakpoint": "breakpoint() drops into a debugger with full process access",
    "exit": "exit() terminates the host process",
    "quit": "quit() terminates the host process",
    "globals": "globals() exposes the module namespace",
    "locals": "locals() exposes the enclosing namespace",
    "vars": "vars() exposes an object's __dict__",
    "dir": "dir() enumerates attributes for use in dynamic lookups",
    "help": "help() imports arbitrary modules via pydoc",
    "memoryview": "memoryview() enables raw buffer manipulation",
    "super": "super() reaches base classes that may be blocked types",
    "object": "bare object() is used to reach __subclasses__ chains",
    "staticmethod": "descriptor construction is not needed in scripts",
    "classmethod": "descriptor construction is not needed in scripts",
    "property": "descriptor construction is not needed in scripts",
    "copyright": "site builtins expose module internals",
    "credits": "site builtins expose module internals",
    "license": "site builtins expose module internals",
}

#: `getattr`/`setattr`/`delattr` are allowed *only* with a literal string name,
#: because a computed name defeats every attribute check in this module.
_LITERAL_ONLY_ATTR_CALLS: Final[frozenset[str]] = frozenset(
    {"getattr", "setattr", "delattr", "hasattr"}
)


# --- attribute policy -----------------------------------------------------

#: Attribute names that are never legal anywhere. The standard sandbox-escape
#: ladder: from any object you can reach its type, its type's bases, every
#: subclass loaded in the process, and from there a function whose __globals__
#: contains the real builtins.
_FORBIDDEN_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "__class__", "__bases__", "__base__", "__subclasses__", "__mro__",
        "mro", "__globals__", "__code__", "__closure__", "__func__",
        "__self__", "__builtins__", "__dict__", "__getattribute__",
        "__getattr__", "__setattr__", "__delattr__", "__reduce__",
        "__reduce_ex__", "__init_subclass__", "__subclasshook__",
        "__import__", "__loader__", "__spec__", "__package__", "__file__",
        "__path__", "__module__", "__qualname__", "__wrapped__",
        "func_globals", "func_code", "func_closure", "gi_frame", "cr_frame",
        "f_globals", "f_locals", "f_builtins", "f_back", "tb_frame",
        "__objclass__", "__weakref__", "__annotations__", "__defaults__",
        "__kwdefaults__", "__new__", "__init__", "__call__",
        "__getstate__", "__setstate__", "__sizeof__", "__format__",
        "__doc__",
    }
)

#: Dotted bpy paths that are blocked. Matched against the resolved attribute
#: chain, so `bpy.app.handlers.frame_change_post` is caught by the
#: `bpy.app.handlers` prefix. Unlike the desktop policy, render/save/open and
#: import/export paths are absent on purpose — they are what this tool is for.
_FORBIDDEN_BPY_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("bpy.app.driver_namespace", "driver_namespace injects globals into driver eval"),
    ("bpy.app.handlers", "handler registration persists code past this script"),
    ("bpy.app.timers", "timers persist code past this script"),
    ("bpy.app.binary_path", "exposes the Blender executable path for re-launch"),
    ("bpy.utils.register_class", "class registration persists code past this script"),
    ("bpy.utils.unregister_class", "class registration persists code past this script"),
    ("bpy.utils.register_classes_factory", "class registration persists code"),
    ("bpy.utils.execfile", "executes a file from disk"),
    ("bpy.utils.load_scripts", "executes scripts from disk"),
    ("bpy.utils.script_paths", "enumerates disk locations for script loading"),
    ("bpy.utils.user_resource", "resolves writable on-disk resource paths"),
    ("bpy.utils.modules_from_path", "imports arbitrary modules from disk"),
    ("bpy.utils.refresh_script_paths", "reloads scripts from disk"),
    ("bpy.data.texts", "text datablocks are an execution path (Run Script)"),
    ("bpy.data.scripts", "script datablocks are an execution path"),
    ("bpy.data.libraries", "library loading links external .blend files, which can carry code"),
    ("bpy.props", "property registration persists definitions past this script"),
    ("bpy.types.Operator", "defining operators registers persistent code"),
    ("bpy.types.Panel", "defining panels registers persistent UI code"),
    ("bpy.types.AddonPreferences", "addon preference classes persist"),
    ("bpy.types.Macro", "macros chain operator execution"),
    # Loading attacker-supplied datablocks from another .blend is a code
    # execution path: the file can carry drivers and handlers that Blender
    # evaluates on load.
    ("bpy.ops.wm.append", "appends datablocks from an external .blend (code can ride along)"),
    ("bpy.ops.wm.link", "links datablocks from an external .blend (code can ride along)"),
    ("bpy.ops.wm.lib_relocate", "repoints a library at an arbitrary .blend"),
    ("bpy.ops.wm.lib_reload", "reloads a library from disk"),
    ("bpy.ops.wm.save_homefile", "overwrites the user's startup file"),
    ("bpy.ops.wm.url_open", "opens a URL in the user's browser"),
    ("bpy.ops.wm.path_open", "opens a path with the OS handler"),
    ("bpy.ops.wm.console_toggle", "exposes an interactive Python console"),
    ("bpy.ops.wm.quit_blender", "terminates the host process"),
    ("bpy.ops.render.play_rendered_anim", "launches an external player process"),
    # A new entry in the script search path is a persistent code-loading
    # location: Blender imports startup/, addons/ and modules/ from it on
    # the next launch. This is the live 3.6+ collection; the pre-3.6 string
    # property `script_directory` is covered by `_FORBIDDEN_ASSIGN_ATTRS`.
    (
        "bpy.context.preferences.filepaths.script_directories",
        "adds a persistent script search path Blender loads code from",
    ),
)

#: Whole `bpy.ops` submodule prefixes that are blocked because every operator
#: under them executes code. Prefix matching fails closed for operators that
#: do not exist yet.
_FORBIDDEN_OPS_PREFIXES: Final[tuple[str, ...]] = (
    "bpy.ops.script",       # bpy.ops.script.* executes python
    "bpy.ops.text",         # bpy.ops.text.* runs text datablocks
    "bpy.ops.preferences",  # preferences ops install and enable addons
    "bpy.ops.extensions",   # 4.2+ extension ops install and enable packages
    "bpy.ops.console",      # the console executes arbitrary python
)

#: Attribute names whose only purpose is to navigate from a module toward the
#: still-blocked surface. Reading one of these off a receiver we could not
#: resolve is refused outright — see `_fail_on_unrooted_navigation`. Smaller
#: than the desktop set because most operator namespaces are now allowed and
#: need no backstop (and names like `render` are ordinary data attributes:
#: `bpy.data.scenes[0].render` must keep working).
_MODULE_NAVIGATION: Final[frozenset[str]] = frozenset({
    "ops", "utils", "app", "props", "types",
    # Sub-namespaces of bpy.ops that own the blocked operators.
    "wm", "script", "preferences", "extensions",
})

#: Attribute names that are blocked wherever they appear, regardless of what
#: they hang off, because the receiver cannot always be resolved statically:
#: `d = bpy.data; d.texts[...]` would otherwise slip past the dotted-path
#: check. This is the deliberate false-positive cost of a static analyzer
#: without type inference.
_FORBIDDEN_BARE_ATTRS: Final[dict[str, str]] = {
    "driver_namespace": "driver_namespace injects globals into driver expression eval",
    "register_class": "class registration persists code past this script",
    "unregister_class": "class registration persists code past this script",
    "execfile": "executes a file from disk",
    "load_scripts": "executes scripts from disk",
    "save_homefile": "overwrites the user's startup file",
    "quit_blender": "terminates the host process",
    "url_open": "opens a URL in the user's browser",
    "path_open": "opens a path with the OS handler",
    "console_toggle": "exposes an interactive Python console",
    "as_pointer": "leaks a raw memory address usable with ctypes",
    "driver_add": "drivers evaluate python expressions on every frame",
    "driver_remove": "driver manipulation is part of the driver eval surface",
    "texts": "text datablocks are an execution path (Run Script)",
    "scripts": "script datablocks are an execution path",
    "libraries": "library loading links external .blend files, which can carry code",
    "handlers": "handler registration persists code past this script",
    "timers": "timers persist code past this script",
    "app_handlers": "handler registration persists code past this script",
    "binary_path": "exposes the Blender executable path for re-launch",
    "user_resource": "resolves writable on-disk resource paths",
    "script_paths": "enumerates disk locations for script loading",
    "script_directories": "adds a persistent script search path Blender loads code from",
    "modules_from_path": "imports arbitrary modules from disk",
    "python_file_run": "executes a python file from disk",
    "run_script": "executes a text datablock as python",
    "addon_install": "installs an addon from disk",
    "addon_enable": "enables an addon, executing its module-level code",
}

#: Assigning to any of these on any object creates a stored python expression
#: that Blender evaluates later, outside this validator's reach. `.filepath`
#: and `.mode` are deliberately assignable here (render output paths, script
#: node modes are ordinary work); the dangerous script-node combination still
#: requires a text datablock or `.script` assignment, both of which are blocked.
_FORBIDDEN_ASSIGN_ATTRS: Final[dict[str, str]] = {
    "expression": "driver expressions are evaluated by Blender as python",
    "script": "script nodes execute their assigned datablock",
    "use_self": "enables driver expression access to the owning datablock",
    # Pre-3.6 spelling of the script search path (a single string). 3.6+
    # exposes the `script_directories` collection instead, which is blocked
    # on access above; keeping this entry costs nothing on older builds.
    "script_directory": "redirects Blender's script search path",
    "use_scripts_auto_execute": "enables automatic execution of embedded scripts",
}


# --- node policy ----------------------------------------------------------

#: Statement and expression node types a script may contain. Everything not
#: listed raises. Notable exclusions and why:
#:   Import*      handled separately (allowlisted modules only)
#:   Lambda       anonymous indirection that defeats call-target resolution
#:   ClassDef     class bodies are the natural home for registered bpy types
#:   Global/Nonlocal  rebinding module-scope names to smuggle capabilities
#:   Await/Async* no event loop in Blender's main thread; pure attack surface
#:   NamedExpr    walrus lets an expression bind a name mid-condition, which
#:                makes call-target tracking unreliable for little real benefit
_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Delete,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.ExceptHandler,
    ast.Raise,
    ast.Assert,
    ast.With,
    ast.withitem,
    ast.FunctionDef,
    ast.Return,
    ast.arguments,
    ast.arg,
    ast.keyword,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.Constant,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Starred,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    # operators are leaf nodes with no behavior of their own
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.And, ast.Or, ast.Not, ast.UAdd, ast.USub, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())  # 3.11+

_ALLOWED_NODE_SET: Final[frozenset[type[ast.AST]]] = frozenset(_ALLOWED_NODES)

#: Node types worth naming in the error message rather than reporting as a bare
#: "construct not allowed", because the author needs to know it was deliberate.
_NODE_EXPLANATIONS: Final[dict[str, str]] = {
    "Lambda": "lambdas are indirection that hides the real call target; use def",
    "ClassDef": "class definitions are the registration path for persistent bpy types",
    "Global": "global rebinds module-scope names",
    "Nonlocal": "nonlocal rebinds enclosing-scope names",
    "Import": "import is validated separately; this import is not allowed",
    "ImportFrom": "from-import is validated separately; this import is not allowed",
    "AsyncFunctionDef": "async has no event loop in Blender's main thread",
    "AsyncFor": "async has no event loop in Blender's main thread",
    "AsyncWith": "async has no event loop in Blender's main thread",
    "Await": "async has no event loop in Blender's main thread",
    "Yield": "generators defer execution past validation",
    "YieldFrom": "generators defer execution past validation",
    "NamedExpr": "walrus assignment obscures call-target analysis",
    "Match": "structural pattern matching can bind names implicitly",
}

#: Source-level limits. A script that trips these is either generated garbage
#: or an attempt to exhaust the parser, and neither should reach Blender.
MAX_CODE_BYTES: Final[int] = 200_000
MAX_AST_NODES: Final[int] = 20_000
MAX_NESTING_DEPTH: Final[int] = 24


def _attr_chain(node: ast.AST) -> str | None:
    """Resolve a dotted access into `a.b.c`, or None if it is not static.

    Only chains rooted at a plain Name resolve. `foo()[0].bar` returns None,
    which is precisely why `_FORBIDDEN_BARE_ATTRS` exists as a backstop: an
    unresolvable receiver means we cannot prove the access is safe.
    """
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _path_is_blocked(path: str) -> str | None:
    """Return the reason `path` (a resolved dotted chain) is forbidden."""
    for prefix, reason in _FORBIDDEN_BPY_PATHS:
        # Exact match or a deeper access underneath the blocked node.
        if path == prefix or path.startswith(prefix + "."):
            return reason
    for prefix in _FORBIDDEN_OPS_PREFIXES:
        if path == prefix or path.startswith(prefix + "."):
            return f"{prefix}.* executes code or installs addons"
    return None


def _collect_bindings(node: ast.AST) -> set[str]:
    """Names bound anywhere inside `node`, including nested scopes.

    Deliberately over-approximate: it does not model Python's scope rules, so
    a name bound in a sibling function counts as known here. That is
    acceptable because binding a name is not itself a capability — every
    *use* of a name still passes the attribute, call, and path checks, and
    every binding still passes `_bind`'s shadowing rules.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            found.add(child.id)
        elif isinstance(child, ast.arg):
            found.add(child.arg)
        elif isinstance(child, ast.FunctionDef):
            found.add(child.name)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            found.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                if alias.name != "*":
                    found.add(alias.asname or alias.name.split(".")[0])
    return found


class _Validator(ast.NodeVisitor):
    """Deny-by-default walk over a pre-scanned binding set.

    Tracks locally bound names only to *reject* shadowing of module names, not
    to grant anything: rebinding `bpy = something_else` would let a script make
    a blocked dotted path unresolvable, so we forbid the rebind instead of
    trying to follow it.
    """

    def __init__(self) -> None:
        self.imported: set[str] = set()
        self.bound: set[str] = set()
        #: Names bound by a `def`. Only these (plus builtins, modules, and
        #: from-imports) may be used as a bare call target — see `visit_Call`.
        self.functions: set[str] = set()
        #: Names bound by `from module import name`. Calling one is equivalent
        #: to the always-allowed `module.name(...)` attribute call, so they are
        #: valid bare call targets (`from mathutils import Vector; Vector(...)`
        #: is the single most common shape in real bpy scripts).
        self.from_imported: set[str] = set()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _fail(message: str, node: ast.AST) -> None:
        raise SandboxViolation(message, getattr(node, "lineno", None))

    def _bind(self, name: str, node: ast.AST) -> None:
        """Record a new local name, refusing to shadow an imported module."""
        if name in self.imported:
            self._fail(
                f"cannot rebind imported module name {name!r}; "
                "shadowing modules defeats path-based checks",
                node,
            )
        if name in _FORBIDDEN_CALLABLES or name in _LITERAL_ONLY_ATTR_CALLS:
            self._fail(
                f"cannot define {name!r}; rebinding a blocked builtin name is "
                "how a script smuggles the real one back in",
                node,
            )
        if name in ALLOWED_BUILTINS:
            # Shadowing `print` or `len` is not itself an escape, but it makes
            # every later call to that name mean something this validator did
            # not check. Rejecting it keeps "a call to an allowlisted builtin
            # is a call to the real builtin" true, which several other rules
            # rely on.
            self._fail(
                f"cannot rebind builtin name {name!r}; shadowing makes later "
                "calls to it unverifiable",
                node,
            )
        if name.startswith("__") and name.endswith("__"):
            self._fail(f"cannot bind dunder name {name!r}", node)
        self.bound.add(name)

    # -- structural gate --------------------------------------------------

    def generic_visit(self, node: ast.AST) -> None:
        """Every node passes through here; unknown types are fatal."""
        if type(node) not in _ALLOWED_NODE_SET:
            name = type(node).__name__
            reason = _NODE_EXPLANATIONS.get(name, f"{name} is not on the allowlist")
            self._fail(reason, node)
        super().generic_visit(node)

    # -- imports ----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_MODULES:
                self._fail(f"import of {alias.name!r} is not allowed", node)
            if alias.name in _DENIED_SUBMODULES:
                self._fail(f"import of {alias.name!r} is not allowed", node)
            if alias.asname:
                # Aliasing hides the module behind a name the path checks do
                # not know about, so `import bpy as b; b.ops.script...` would
                # resolve to an unrecognized chain.
                self._fail(
                    f"import aliasing ({alias.name} as {alias.asname}) is not "
                    "allowed; it hides the module from path checks",
                    node,
                )
            self.imported.add(root)
        # Do not generic_visit: alias nodes are intentionally not on the
        # node allowlist, and everything about them has been checked here.

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._fail("relative imports are not allowed", node)
        module = node.module or ""
        root = module.split(".")[0]
        if root not in ALLOWED_MODULES or module in _DENIED_SUBMODULES:
            self._fail(f"import from {module!r} is not allowed", node)
        if root == "bpy":
            # `from bpy import ops` rebinds a namespace to a name the dotted
            # rules do not know, so `ops.wm.append(...)` would bypass every
            # `bpy.ops.*` check. Whole-module import keeps chains checkable.
            self._fail(
                "from-imports of bpy are not allowed; use `import bpy` and "
                "full dotted paths so they can be checked",
                node,
            )
        for alias in node.names:
            if alias.name == "*":
                self._fail("wildcard import hides what enters the namespace", node)
            if alias.name in _DENIED_IMPORT_NAMES:
                self._fail(
                    f"importing {alias.name!r} from {module!r} is not allowed", node
                )
            if alias.name.startswith("_"):
                self._fail(f"importing private name {alias.name!r} is not allowed", node)
            self._bind(alias.asname or alias.name, node)

    # -- names ------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        name = node.id
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(name, node)
            return
        # Load context: the name must be something we handed out.
        if name in _FORBIDDEN_CALLABLES:
            self._fail(f"{name} is forbidden: {_FORBIDDEN_CALLABLES[name]}", node)
        if name.startswith("__") and name.endswith("__"):
            self._fail(f"dunder name {name!r} is not accessible", node)
        if name in self.imported or name in self.bound:
            return
        if name in ALLOWED_BUILTINS or name in _LITERAL_ONLY_ATTR_CALLS:
            return
        self._fail(
            f"unknown name {name!r}; only allowlisted builtins, imported "
            "modules, and names bound in this script may be used",
            node,
        )

    # -- attributes -------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if attr in _FORBIDDEN_ATTRS:
            self._fail(f"attribute {attr!r} is an interpreter escape path", node)
        if attr.startswith("__") and attr.endswith("__"):
            # Catches dunders invented after this file was written.
            self._fail(f"dunder attribute {attr!r} is not accessible", node)
        if attr in _FORBIDDEN_BARE_ATTRS:
            self._fail(f"{attr!r}: {_FORBIDDEN_BARE_ATTRS[attr]}", node)
        # Writing to a stored-expression attribute is what arms a driver.
        if isinstance(node.ctx, (ast.Store, ast.Del)) and attr in _FORBIDDEN_ASSIGN_ATTRS:
            self._fail(f"assigning {attr!r}: {_FORBIDDEN_ASSIGN_ATTRS[attr]}", node)
        path = _attr_chain(node)
        if path is None:
            # The chain does not bottom out at a plain Name, so the dotted
            # rules below are unreachable. That is a bypass, not a safe case:
            # `[bpy][0].ops.script.python_file_run(...)` wraps the module in a
            # container so `_attr_chain` returns None and the path rules are
            # skipped entirely. Fail closed on the navigation segments that
            # lead to the blocked surface; legitimate scripts reach `bpy.ops`
            # through a plain name.
            self._fail_on_unrooted_navigation(node)
        if path is not None:
            reason = _path_is_blocked(path)
            if reason:
                self._fail(f"{path} is forbidden: {reason}", node)
            # Assigning onto an RNA type (`bpy.types.Scene.foo = ...`) registers
            # a property that outlives this script. Reading `bpy.types.X` stays
            # allowed because `isinstance(o, bpy.types.Mesh)` is ordinary code.
            if isinstance(node.ctx, (ast.Store, ast.Del)) and path.startswith(
                "bpy.types."
            ):
                self._fail(
                    f"assigning to {path} registers a persistent RNA property",
                    node,
                )
        self.generic_visit(node)

    def _fail_on_unrooted_navigation(self, node: ast.Attribute) -> None:
        """Reject module-navigation attributes on an unresolvable receiver.

        Called only when `_attr_chain` could not prove what the chain is rooted
        in. `_MODULE_NAVIGATION` names the segments that exist to *reach* the
        blocked surface. Ordinary data access is unaffected: `objs[0].location`
        and `bpy.data.scenes[0].render` navigate *data*, not modules, so their
        leaves are not in this set.
        """
        if node.attr in _MODULE_NAVIGATION:
            self._fail(
                f"{node.attr!r} reached through an unresolvable receiver; "
                "module navigation must start from a plain name so it can be "
                "checked against the path rules",
                node,
            )

    # -- calls ------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Name):
            name = func.id
            if name in _FORBIDDEN_CALLABLES:
                self._fail(f"{name}() is forbidden: {_FORBIDDEN_CALLABLES[name]}", node)
            if name in _LITERAL_ONLY_ATTR_CALLS:
                self._check_literal_attr_call(name, node)
            # `type(x)` is harmless introspection; `type(name, bases, ns)` is a
            # class factory, which is how a script builds a registrable bpy
            # type without a ClassDef statement.
            if name == "type" and len(node.args) != 1:
                self._fail(
                    "type() with three arguments creates a class dynamically",
                    node,
                )
            # A bare name is only a checkable call target if we know what it
            # holds: a script-defined function, an imported module, or an
            # allowlisted builtin. A name bound by iteration or unpacking
            # (`for f in fns: f()`) launders whatever the container held past
            # every check above.
            elif not (
                name in self.functions
                or name in self.imported
                or name in self.from_imported
                or name in ALLOWED_BUILTINS
                # Already validated above by `_check_literal_attr_call`.
                or name in _LITERAL_ONLY_ATTR_CALLS
            ):
                self._fail(
                    f"{name!r} is not a known callable; call targets must be a "
                    "def in this script, an allowlisted builtin, or an "
                    "attribute of an imported module",
                    node,
                )
        elif not isinstance(func, ast.Attribute):
            # `(lambda: ...)()`, `f()()`, `fns[0]()` — the callee is produced by
            # an expression, so no static check can say what actually runs.
            self._fail(
                "call target must be a name or an attribute; computed call "
                "targets cannot be validated",
                node,
            )

        self.generic_visit(node)

    def _check_literal_attr_call(self, name: str, node: ast.Call) -> None:
        """`getattr`/`setattr`/`delattr`/`hasattr` must name a literal attribute.

        A computed name (`getattr(bpy, "ap" + "p")`) is rejected outright
        rather than constant-folded: partial evaluation is a losing game
        against an attacker who can nest arbitrary expressions.
        """
        if len(node.args) < 2:
            self._fail(f"{name}() requires an explicit literal attribute name", node)
        target = node.args[1]
        if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
            self._fail(
                f"{name}() attribute name must be a literal string, not a "
                "computed expression",
                node,
            )
        attr = target.value
        if attr in _FORBIDDEN_ATTRS or (attr.startswith("__") and attr.endswith("__")):
            self._fail(f"{name}() targets escape attribute {attr!r}", node)
        if attr in _MODULE_NAVIGATION:
            # `getattr(bpy, 'ops')` hands out a namespace object the dotted
            # rules can no longer see — same laundering as `o = bpy.ops`.
            self._fail(
                f"{name}() targets module namespace {attr!r}; namespaces may "
                "only be reached through a checkable dotted path",
                node,
            )
        if attr in _FORBIDDEN_BARE_ATTRS:
            self._fail(f"{name}() targets {attr!r}: {_FORBIDDEN_BARE_ATTRS[attr]}", node)
        if name in ("setattr", "delattr") and attr in _FORBIDDEN_ASSIGN_ATTRS:
            self._fail(f"{name}() targets {attr!r}: {_FORBIDDEN_ASSIGN_ATTRS[attr]}", node)

    # -- functions --------------------------------------------------------

    def visit_arg(self, node: ast.arg) -> None:
        """Parameter names are bindings and follow the same shadowing rules."""
        self._bind(node.arg, node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.decorator_list:
            # A decorator is a call whose target is applied to the function
            # object itself — the classic route to bpy.app.handlers.persistent
            # and to registration helpers.
            self._fail(
                "decorators are not allowed; they apply arbitrary callables to "
                "function objects",
                node,
            )
        self._bind(node.name, node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, node)
        self.generic_visit(node)


def _check_module_value_use(tree: ast.AST, imported: set[str]) -> None:
    """Refuse to let a module or bpy namespace object escape into a value.

    The dotted-path rules are only sound while a blocked path is spelled as a
    chain rooted at the module name. Handing the namespace itself to a variable,
    argument, or container re-roots later chains at an arbitrary name and every
    `bpy.ops.*` rule goes blind:

        o = bpy.ops;  o.wm.append(filepath="evil.blend")
        def f(m): m.wm.link(filepath="evil.blend")
        f(bpy.ops)

    `append` and `link` cannot be leaf-blocked (`list.append`, collection
    `.link` are essential), so the laundering itself is refused instead: an
    imported module name may appear only as the root of an attribute chain, and
    a chain that stops inside the navigation namespace (`bpy.ops`, `bpy.ops.wm`)
    may not be used as a value at all — it must continue to a concrete endpoint.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def _is_chain_root(node: ast.AST) -> bool:
        parent = parents.get(node)
        return isinstance(parent, ast.Attribute) and parent.value is node

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in imported
            and not _is_chain_root(node)
        ):
            raise SandboxViolation(
                f"module {node.id!r} may only be used as the start of a dotted "
                "path; passing the module object around defeats path checks",
                getattr(node, "lineno", None),
            )
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and not _is_chain_root(node)  # only the outermost node of a chain
        ):
            path = _attr_chain(node)
            if path is None:
                continue
            parts = path.split(".")
            if (
                parts[0] in imported
                and len(parts) > 1
                and all(p in _MODULE_NAVIGATION for p in parts[1:])
            ):
                raise SandboxViolation(
                    f"{path} is a module namespace and may not be used as a "
                    "value; continue the path to a concrete attribute or call",
                    getattr(node, "lineno", None),
                )


def _guard_source(code: str) -> ast.Module:
    """Parse with size limits applied before and after."""
    if not isinstance(code, str):
        raise SandboxViolation(f"code must be a string, got {type(code).__name__}")
    if len(code.encode("utf-8", errors="replace")) > MAX_CODE_BYTES:
        raise SandboxViolation(f"script exceeds {MAX_CODE_BYTES} bytes")
    if "\x00" in code:
        raise SandboxViolation("script contains a NUL byte")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SandboxViolation(f"syntax error: {exc.msg}", exc.lineno) from exc
    except (ValueError, MemoryError, RecursionError) as exc:
        # ast.parse raises ValueError on some malformed literals and
        # RecursionError on pathologically nested input.
        raise SandboxViolation(f"unparseable script: {type(exc).__name__}") from exc

    count = 0
    for _ in ast.walk(tree):
        count += 1
        if count > MAX_AST_NODES:
            raise SandboxViolation(f"script exceeds {MAX_AST_NODES} AST nodes")
    _check_depth(tree)
    return tree


def _check_depth(tree: ast.AST) -> None:
    """Reject deeply nested expressions.

    Depth is a proxy for obfuscation: no hand-written Blender script nests two
    dozen levels, but generated payloads that try to exhaust an analyzer do.
    Iterative so that checking the depth cannot itself blow the stack.
    """
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_NESTING_DEPTH:
            raise SandboxViolation(
                f"script nests deeper than {MAX_NESTING_DEPTH} levels",
                getattr(node, "lineno", None),
            )
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1))


def validate_code(code: str) -> None:
    """Raise `SandboxViolation` unless `code` clears the safe-mode policy.

    Deny by default: the walk rejects any AST node type, name, attribute, or
    call target that is not explicitly allowlisted above. Returns None on
    success so callers can use it as an assertion.
    """
    tree = _guard_source(code)

    validator = _Validator()
    # Imports are resolved first so that `_bind` can reject any later attempt to
    # shadow a module name, no matter where in the file the shadow appears.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.asname:
                    validator.imported.add(alias.name.split(".")[0])
    # Then every name the script binds anywhere, so a load is judged on whether
    # the script defines the name at all rather than on visit order.
    validator.bound = _collect_bindings(tree) - validator.imported
    # Function and from-import names are collected separately because they are
    # valid bare call targets; a forward reference (`a()` defined above `b()`,
    # a call above the import line inside a def) must work. Every ImportFrom
    # node is still fully validated by visit_ImportFrom before this pre-pass
    # can matter: an illegal from-import fails the walk regardless.
    validator.functions = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    validator.from_imported = {
        alias.asname or alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for alias in n.names
        if alias.name != "*"
    }
    _check_module_value_use(tree, validator.imported)
    validator.visit(tree)


def is_safe(code: str) -> tuple[bool, str]:
    """Non-raising variant of `validate_code` -> `(ok, reason)`.

    `reason` is empty when ok, otherwise the violation message — safe to
    return to the model so it can repair its script.
    """
    try:
        validate_code(code)
    except SandboxViolation as exc:
        return False, str(exc)
    return True, ""
