"""Workflow generation: one template per workflow, rendered for each space scope.

A template is the workflow YAML plus a few line directives. Directives start with
`#%` (after any indentation) and never reach the output, so they can't collide with
the Liquid `{{ }}` and `{% %}` that Kibana evaluates at run time:

    #% var NAME global=TEXT space=TEXT   define [[NAME]] for each scope
                                        (the installer also passes values, e.g. [[space_id]])
    #% if global | #% if space           keep the lines up to #% else / #% end
    #% else                             in that scope only
    #% indent global N | space N         indent the lines up to #% end by N spaces
    #% end                              in that scope only
    #% # TEXT                           a note for template readers

Scopes:
    global   one copy reads rules in every space (today's "all-spaces" workflow)
    space    one copy per space (today's "per-space" workflow); render_workflow fills
             in the space ID and the shared-stats switch afterwards

Standard library only, like the rest of the installer.
"""

from __future__ import annotations

import re

from .core import PACK_ROOT, WORKFLOW_FILES, WORKFLOW_TEMPLATE, ConfigError

SCOPES = ("global", "space")

# Installer workflow modes (the --workflow option) and the scope each renders.
MODE_SCOPES = {"all-spaces": "global", "per-space": "space"}

_VAR_RE = re.compile(r"\[\[([a-z_][a-z0-9_]*)\]\]")


class TemplateError(ConfigError):
    pass


def render_template(text: str, scope: str, source: str = "template",
                    params: dict[str, str] | None = None) -> str:
    """Render a workflow template for one scope.

    params are values the installer supplies, such as space_id for a space scope;
    [[name]] uses them unless a #% var of the same name is defined."""
    if scope not in SCOPES:
        raise TemplateError(f"{source}: unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    variables: dict[str, str] = dict(params or {})
    # Each open block: [kind, keeping lines?, extra indent, line number]
    stack: list[list] = []
    out: list[str] = []

    def fail(lineno: int, msg: str):
        raise TemplateError(f"{source}:{lineno}: {msg}")

    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.strip()
        if stripped.startswith("#%"):
            words = stripped[2:].split()
            if not words:
                fail(lineno, "empty directive")
            cmd, args = words[0], words[1:]
            if cmd.startswith("#"):
                continue
            if cmd == "var":
                if len(args) < 2:
                    fail(lineno, "var needs a name and global=... space=...")
                name, rest = args[0], stripped[2:].split(None, 2)[2]
                values = dict(re.findall(r"(\w+)=(\S+)", rest))
                if set(values) != set(SCOPES):
                    fail(lineno, f"var {name} must give a value for each of {', '.join(SCOPES)}")
                variables[name] = values[scope]
            elif cmd == "if":
                if len(args) != 1 or args[0] not in SCOPES:
                    fail(lineno, f"if needs one scope: {' or '.join(SCOPES)}")
                stack.append(["if", args[0] == scope, 0, lineno])
            elif cmd == "else":
                if not stack or stack[-1][0] != "if":
                    fail(lineno, "else without if")
                stack[-1][0] = "else"
                stack[-1][1] = not stack[-1][1]
            elif cmd == "indent":
                if len(args) != 2 or args[0] not in SCOPES or not args[1].isdigit():
                    fail(lineno, "indent needs a scope and a number of spaces")
                stack.append(["indent", True, int(args[1]) if args[0] == scope else 0, lineno])
            elif cmd == "end":
                if not stack:
                    fail(lineno, "end without an open block")
                stack.pop()
            else:
                fail(lineno, f"unknown directive {cmd!r}")
            continue
        if not all(block[1] for block in stack):
            continue
        pad = sum(block[2] for block in stack)
        if pad and line.strip():
            line = " " * pad + line

        def sub(m: re.Match) -> str:
            if m.group(1) not in variables:
                fail(lineno, f"[[{m.group(1)}]] is not defined")
            return variables[m.group(1)]

        out.append(_VAR_RE.sub(sub, line))
    if stack:
        fail(stack[-1][3], f"{stack[-1][0]} is never closed")
    return "".join(out)


def render_file(rel: str, scope: str, params: dict[str, str] | None = None) -> str:
    return render_template((PACK_ROOT / rel).read_text(), scope, source=rel, params=params)


def generated_workflows() -> dict[str, str]:
    """The checked-in workflow files and the text the template renders for each.

    The files stay in the repo for anyone importing a workflow by hand; `render`
    rewrites them and `lint` fails when they've drifted from the template."""
    return {path: render_file(WORKFLOW_TEMPLATE, MODE_SCOPES[mode]) for mode, path in WORKFLOW_FILES.items()}


def stale_workflows() -> list[str]:
    """Checked-in workflow files that differ from what the template renders."""
    out = []
    for path, text in generated_workflows().items():
        f = PACK_ROOT / path
        if not f.exists() or f.read_text() != text:
            out.append(path)
    return out


def write_workflows() -> list[str]:
    """Rewrite the checked-in workflow files from the template. Returns the files changed."""
    changed = stale_workflows()
    for path, text in generated_workflows().items():
        if path in changed:
            (PACK_ROOT / path).write_text(text)
    return changed
