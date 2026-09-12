"""The layering, asserted rather than remembered.

Both rules here were kept by hand during the extraction of `core.py`, and
both were nearly broken by hand. A refactor that distributes a god object
across files while keeping its coupling has cost more than it bought, so the
invariant that makes the difference is worth a test.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

HELM = pathlib.Path(__file__).resolve().parent.parent / "helm"
TESTS = pathlib.Path(__file__).resolve().parent

#: Everything below `core` in the layering. `core` imports these; none of
#: them may import `core`, directly or by way of each other.
LAYERED = (
    "errors.py",
    "paths.py",
    "values.py",
    "state.py",
    "git.py",
    "authority.py",
    "processes.py",
    "discovery.py",
    "launching.py",
    "policy.py",
    "coordinator/base.py",
    "coordinator/archive.py",
    "coordinator/pull_requests.py",
    "coordinator/ledger.py",
    "coordinator/adopt.py",
    "coordinator/knowledge.py",
    "coordinator/status.py",
    "coordinator/skills.py",
    "coordinator/gates.py",
    "coordinator/decisions.py",
    "coordinator/lifecycle.py",
    "coordinator/protection.py",
    "coordinator/foremen.py",
    "coordinator/workers.py",
    "coordinator/caller.py",
    "coordinator/learning.py",
    "coordinator/health.py",
    "coordinator/agents.py",
    "coordinator/launch.py",
)


def _imports(path: pathlib.Path) -> set[str]:
    """Every module this file imports, as dotted names relative to `helm`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from ..git import x` inside helm/coordinator/ is helm.git;
                # the level tells us how far up, and what matters here is only
                # whether the last hop names `core`.
                found.update(
                    f"{node.module or ''}.{alias.name}".strip(".")
                    for alias in node.names
                )
                if node.module:
                    found.add(node.module)
            else:
                found.add(node.module or "")
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


class NothingBelowCoreImportsCoreTests(unittest.TestCase):
    def test_no_layered_module_imports_core(self) -> None:
        """The one rule that keeps this a refactor rather than a rename.

        A module that imports `core` back has not been extracted; it has been
        moved, and the coupling it was extracted to break is still there --
        only now it is spread over more files, which is worse than leaving it
        alone.
        """
        offenders = []
        for relative in LAYERED:
            path = HELM / relative
            if not path.exists():
                continue
            for name in _imports(path):
                if name == "core" or name.endswith(".core") or name.startswith("helm.core"):
                    offenders.append(f"{relative} imports {name}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_list_of_layered_modules_is_complete(self) -> None:
        """A guard that has to be remembered is a guard that rots.

        Every coordinator submodule is below core by construction, so a new
        one that nobody adds to `LAYERED` would be silently unchecked -- which
        is worse than no check, because the suite would still be green.
        """
        on_disk = {
            f"coordinator/{path.name}"
            for path in (HELM / "coordinator").glob("*.py")
            if path.name != "__init__.py"
        }
        self.assertEqual(
            sorted(on_disk - set(LAYERED)), [],
            "a coordinator submodule is not in LAYERED and so is not checked",
        )

    def test_importing_the_layer_does_not_pull_in_core(self) -> None:
        """The same rule at runtime, in case an import hides behind a name."""
        import subprocess
        import sys

        modules = ", ".join(
            "helm." + relative[:-3].replace("/", ".") for relative in LAYERED
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys, {modules}\n"
                "raise SystemExit(1 if 'helm.core' in sys.modules else 0)",
            ],
            cwd=str(HELM.parent),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"importing the layered modules pulled in helm.core\n{result.stderr}",
        )


class EveryGlobalAFunctionReachesForExistsTests(unittest.TestCase):
    #: Names the compiler injects into a decorated function's own bytecode.
    #: `@dataclass` builds a `__repr__` that calls `get_ident`, and
    #: `@contextmanager` wraps in `_GeneratorContextManager`; both resolve in
    #: the decorator's module, not in the module being checked.
    DECORATOR_INJECTED = frozenset({"get_ident", "_GeneratorContextManager"})

    def test_no_function_looks_up_a_global_its_module_does_not_have(self) -> None:
        """Catch the import a moved function left behind, before it runs.

        Python resolves a global when the line executes, so a function that
        reaches for a name its module never imported is fine until the branch
        containing it is taken. The suite only finds it if a test walks that
        branch, and the branches that go uncovered are exactly the unlikely
        ones -- retries, error paths, the second attempt.

        `LOAD_GLOBAL` in the compiled bytecode is the exact set of names a
        function will look up in its module, so this needs no scope analysis
        and produces no false positives beyond the two the compiler injects.

        It found `time.sleep(2)` in `cli._worker_runner`'s workspace-check
        retry, in a module that never imported `time` -- a retry that would
        raise NameError instead of retrying, on the one path it exists for.
        """
        import builtins
        import dis
        import glob
        import importlib
        import types

        def code_objects(code: types.CodeType):
            yield code
            for const in code.co_consts:
                if isinstance(const, types.CodeType):
                    yield from code_objects(const)

        modules = [
            path[:-3].replace("/", ".")
            for path in sorted(
                str(p.relative_to(HELM.parent))
                for p in list(HELM.glob("*.py")) + list((HELM / "coordinator").glob("*.py"))
            )
            # `__main__` runs the CLI on import, and `__init__` defines nothing
            if not path.endswith(("__init__.py", "__main__.py"))
        ]
        self.assertGreater(len(modules), 20, "the module scan found almost nothing")

        offenders = []
        for name in modules:
            module = importlib.import_module(name)
            functions = []
            for value in vars(module).values():
                if isinstance(value, types.FunctionType):
                    functions.append(value)
                elif isinstance(value, type) and value.__module__ == name:
                    for attribute in vars(value).values():
                        plain = (
                            attribute.__func__
                            if isinstance(attribute, (staticmethod, classmethod))
                            else attribute
                        )
                        if isinstance(plain, types.FunctionType):
                            functions.append(plain)
            for function in functions:
                if function.__module__ != name:
                    continue
                for code in code_objects(function.__code__):
                    for instruction in dis.get_instructions(code):
                        if instruction.opname != "LOAD_GLOBAL":
                            continue
                        looked_up = instruction.argval
                        if (
                            looked_up in vars(module)
                            or looked_up in dir(builtins)
                            or looked_up in self.DECORATOR_INJECTED
                        ):
                            continue
                        offenders.append(
                            f"{name}.{function.__qualname__} looks up the global "
                            f"{looked_up!r}, which {name} does not define or import"
                        )
        self.assertEqual(sorted(set(offenders)), [], "\n".join(sorted(set(offenders))))


class NoModuleComputesAPathFromWhereItSitsTests(unittest.TestCase):
    def test_no_module_walks_up_from_its_own_file_to_reach_the_package(self) -> None:
        """`Path(__file__).parent.parent` means a different place after a move.

        It read as "the directory holding the helm package" for as long as it
        lived in `helm/core.py`. Moved one level down into
        `helm/coordinator/launch.py` -- verbatim, so line identity saw nothing,
        and `__file__` exists, so the undefined-global check saw nothing -- the
        same expression became `helm/` itself, and every worker launched with
        that on PYTHONPATH died with "No module named helm".

        `helm.paths.package_parent()` derives it from the package, so it is
        correct from any file. Nothing under helm/ should be walking up from
        its own location instead.
        """
        offenders = []
        for path in sorted(
            list(HELM.glob("*.py")) + list((HELM / "coordinator").glob("*.py"))
        ):
            if path.name == "paths.py":
                continue  # where package_parent() legitimately lives
            source = path.read_text(encoding="utf-8")
            # Over the AST, not the text. A regex matches the pattern wherever
            # it appears -- including inside a comment explaining why not to
            # use it. That fired on the very comment written to warn the next
            # reader away from this trap, which makes the guard punish the one
            # behaviour it wants. Only real attribute access counts.
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Attribute) or node.attr != "parent":
                    continue
                if any(
                    isinstance(inner, ast.Name) and inner.id == "__file__"
                    for inner in ast.walk(node.value)
                ):
                    offenders.append(
                        f"{path.name}:{node.lineno} walks up from __file__; "
                        f"use helm.paths.package_parent() so a move cannot change what it means"
                    )
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestsPatchWhereTheCallerLooksTests(unittest.TestCase):
    def test_no_test_patches_a_name_core_no_longer_calls(self) -> None:
        """Reaching into `helm.core` reaches only the callers that live in core.

        `core` re-exports every name the extraction moved, and
        `mock.patch("helm.core.X")` rebinds the name in *core's* namespace --
        so it keeps working for exactly as long as the caller it means to
        affect is core itself. The moment the last such caller moves into a
        mixin, which binds the function in its own namespace, the patch stops
        reaching anything and the test fails for a reason unrelated to what it
        is testing. That is how
        `test_concurrent_base_branch_change_during_resolution_forces_a_retry`
        broke, and the fix was to make the call go through its module --
        `git._resolve_task_base(...)` -- so there is one place to patch
        whichever module the caller ends up in.

        A name core still calls is not flagged: patching core is then correct,
        and rewriting it would be churn for a hypothetical. What is flagged is
        the state that is already wrong.
        """
        import helm.core as core

        core_source = (HELM / "core.py").read_text(encoding="utf-8")
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            # Both ways a test reaches into core to rebind a name: mock.patch
            # by dotted string, and plain attribute assignment on the imported
            # module. The second is how the `_TRUST_CONFIGS` case was written,
            # and a guard that only knew the first missed it.
            reached = set(
                re.findall(r'patch\(\s*"helm\.core\.([A-Za-z_][A-Za-z0-9_]*)', source)
            ) | set(
                re.findall(r'(?:^|[^\w.])(?:helm\.)?core\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)', source, re.M)
            )
            for name in reached:
                home = getattr(getattr(core, name, None), "__module__", None)
                if home is None or home == "helm.core":
                    continue
                # A name core does not mention at all outside its own import
                # block is one core merely re-exports -- rebinding it there
                # reaches nothing.
                body = core_source.split("\nclass Coordinator(", 1)[-1]
                if not re.search(rf"(?<![\w.]){re.escape(name)}\b", body):
                    offenders.append(
                        f"{path.name} patches helm.core.{name}, which is defined in "
                        f"{home} and no longer called anywhere in core.py -- the patch "
                        f"reaches nothing. Call it through its module and patch there."
                    )
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
