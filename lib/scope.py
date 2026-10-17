"""Pure, lexical scope rules. No filesystem access or lock acquisition.

Paths must share a coordinate system (normally relative to the job cwd).
Companions do not expand scheduler locks. Broad artifact rules are intentional;
protected shared/core paths always take precedence over companion allowance.
"""
import fnmatch
import posixpath
from pathlib import PurePosixPath


LOCKS = {
    "package.json": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    "pyproject.toml": ("uv.lock", "poetry.lock"),
    "Cargo.toml": ("Cargo.lock",),
    "go.mod": ("go.sum",),
}
COMPANION_RULES = (
    "同 stem 测试 / same-stem tests: tests/**/test_<stem>*.py, <stem>_test.py, "
    "<stem>.test.ts(x), <stem>.spec.ts(x); e2e specs in the same feature directory",
    "测试数据 / fixtures, __snapshots__, testdata directories and *.snap anywhere",
    "声明文件包目录中的 / __init__.py in declared files' ancestor package directories",
    "同目录 manifest 对应 lock / package.json → package-lock.json, pnpm-lock.yaml, yarn.lock; "
    "pyproject.toml → uv.lock, poetry.lock; Cargo.toml → Cargo.lock; go.mod → go.sum",
    "生成产物 / dist/, build/, static/dist/ anywhere, *.min.js, *.map; "
    "i18n/locales/translations under generated/ or *.generated.*",
)


def normalize(path):
    return posixpath.normpath(str(path))


def companion_patterns(files):
    """Return shell-style patterns; ** is matched lexically by fnmatch (incl /)."""
    patterns = ["*/fixtures/*", "fixtures/*", "*/__snapshots__/*", "__snapshots__/*",
                "*/testdata/*", "testdata/*", "*.snap", "dist/*", "*/dist/*",
                "build/*", "*/build/*", "*.min.js", "*.map"]
    for directory in ("i18n", "locales", "translations"):
        patterns.extend(["generated/*/" + directory + "/*", "generated/" + directory + "/*",
                         "*/generated/" + directory + "/*", "*/generated/*/" + directory + "/*",
                         directory + "/*.generated.*", "*/" + directory + "/*.generated.*",
                         directory + "/generated/*", "*/" + directory + "/generated/*"])
    for raw in files:
        path = PurePosixPath(normalize(raw))
        stem = path.stem
        patterns.extend(["tests/test_" + stem + "*.py", "tests/*/test_" + stem + "*.py",
                         "*/tests/test_" + stem + "*.py", "*/tests/*/test_" + stem + "*.py",
                         stem + "_test.py", "*/" + stem + "_test.py"])
        for suffix in (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"):
            patterns.extend([stem + suffix, "*/" + stem + suffix])
        # E2E JS specs are allowed only below the source feature's directory.
        for suffix in (".spec.js", ".spec.jsx", ".cy.js", ".cy.ts", ".e2e.ts", ".e2e.js"):
            patterns.append(str(path.parent / "e2e" / (stem + suffix)))
            patterns.append(str(path.parent / "e2e" / "*" / (stem + suffix)))
        patterns.extend(str(parent / "__init__.py") for parent in path.parents)
        patterns.extend(str(path.parent / lock) for lock in LOCKS.get(path.name, ()))
    return list(dict.fromkeys(patterns))


def is_companion(path, files):
    return any(fnmatch.fnmatchcase(normalize(path), pattern)
               for pattern in companion_patterns(files))


def scope_report(changed_files, files, shared_files, policy):
    """Report scope without enforcing it.

    files is the complete declaration (own files + own shared_files). The
    shared_files argument is the *external protected set*, not the job's locks.
    Buckets are exclusive except shared_violations, a subset of out_of_scope.
    In flex, out_of_scope still inventories non-companions, but allows changing
    them unless protected. Strict never classifies an undeclared file companion.
    """
    if policy not in ("strict", "companion", "flex"):
        raise ValueError("invalid scope policy: " + str(policy))
    declared = set(normalize(path) for path in files)
    protected = [normalize(path) for path in shared_files]
    result = dict(declared=[], companion=[], out_of_scope=[], shared_violations=[])
    for path in dict.fromkeys(normalize(path) for path in changed_files):
        if path in declared:
            result["declared"].append(path)
            continue
        violation = any(p in (".", "/") or path == p or path.startswith(p + "/")
                        for p in protected)
        if violation:
            result["shared_violations"].append(path)
        bucket = "companion" if (not violation and policy != "strict" and
                                  is_companion(path, files)) else "out_of_scope"
        result[bucket].append(path)
    return result


def narrow_warnings(files, policy, shared_files=()):
    """Submit-time hints only; unknown build layout never rejects a job."""
    warnings = []
    if len(shared_files) > 3:
        warnings.append("many --lock-shared paths serialize sibling jobs on the same repo; consider narrowing shared locks")
    if policy != "strict":
        return warnings
    paths = [PurePosixPath(normalize(path)) for path in files]
    for path in paths:
        if path.name in LOCKS and not any(path.parent / lock in paths for lock in LOCKS[path.name]):
            warnings.append("%s: consider adding its lock file or rely on companion allowance" % path)
        if path.suffix in (".py", ".ts", ".tsx", ".js", ".jsx") and not (
                path.name == "__init__.py" or path.name.startswith("test_") or
                any(part in ("tests", "test", "e2e") for part in path.parts) or
                any(mark in path.name for mark in ("_test.", ".test.", ".spec."))):
            if not any(other != path and other.stem.startswith("test_" + path.stem)
                       or other.name in (path.stem + "_test.py", path.stem + ".test.ts",
                                         path.stem + ".test.tsx", path.stem + ".spec.ts",
                                         path.stem + ".spec.tsx") for other in paths):
                warnings.append("%s: consider adding tests/test_%s.py or rely on companion allowance" %
                                (path, path.stem))
            if path.suffix != ".py" and not any("dist" in other.parts or "build" in other.parts for other in paths):
                warnings.append("%s: frontend build output policy unknown; consider declaring dist output or rely on companion allowance" % path)
    return warnings


def policy_text(policy, protected=()):
    lines = ["### 改动范围策略 (scope policy): " + policy]
    if policy == "strict":
        lines.append("严格 / strict: stay strictly within --files and declared --lock-shared; "
                     "清单外文件必须 STOP BLOCKED，先申请追加路径。")
    else:
        lines.append("本策略覆盖 prompt 其他位置通用的 strictly within --files / STOP BLOCKED 限制，"
                     "允许 companion 文件无需询问 / overrides generic strict wording for companion files.")
        lines.append("声明文件及以下 companion 文件可直接修改 / declared + companion files allowed without asking:")
        lines.extend("- " + rule for rule in COMPANION_RULES)
        lines.append("其他文件仍需 BLOCKED。" if policy == "companion" else
                     "flex: 任务必需的任何非 shared/core 文件均可直接修改 / any required non-shared file may be changed.")
    lines.extend([
        "shared/core 保护优先：未声明的共享/核心文件仍严格受限 / undeclared protected shared/core files remain strict.",
        "其他运行任务的共享保护快照 / protected paths snapshot: " + (", ".join(protected) or "无 / none"),
        "read-only 要求继续有效 / scope allowance does not override read-only.",
        "每个清单外改动（包括 companion）必须在结论独立行 OUT_OF_SCOPE_FILES: 后用 bullet 列出路径及一行原因 / "
        "list every changed file outside the declaration with a one-line reason.",
        "因范围受阻时写独立 STATUS: BLOCKED 行，并写 NEED_FILES: path1, path2 供 dispatcher 重试 / "
        "emit NEED_FILES for scope blocks.",
    ])
    return "\n".join(lines)
