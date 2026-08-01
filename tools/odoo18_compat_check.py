#!/usr/bin/env python3
"""Static Odoo 18.0 compatibility checker for the OpenHRMS18 addons.

Every check here corresponds to a defect class that actually reached
production and showed up in the container logs. The point is to catch the
regression in CI -- before an upgrade -- rather than in an Odoo log at
2am, so this module deliberately has **no Odoo and no third-party
dependency**: it runs on a bare CPython 3.8+ interpreter.

    python tools/odoo18_compat_check.py            # whole repo
    python tools/odoo18_compat_check.py hr_resignation hr_reminder

Exit code is 0 when clean, 1 when any finding is reported.
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

# Dispatcher types accepted by odoo.http in 18.0. 'jsonrpc' is the Odoo 19
# spelling and makes the whole registry fail to load on 18.
VALID_ROUTE_TYPES = {"http", "json"}

# Field kwargs that 18.0 does not understand. Each one is silently ignored
# after logging "unknown parameter", so the intended behaviour never happens.
DEAD_FIELD_KWARGS = {
    "track_visibility": "removed in Odoo 12 -- use tracking=True",
    "String": "capitalised kwarg is not 'string' -- the label is ignored",
    "Digits": "capitalised kwarg is not 'digits'",
    "oldname": "removed in Odoo 13",
    "select": "removed long ago -- use index=True",
    "invisible": "a view attribute, not a field parameter",
    "states": "removed as a field parameter in Odoo 17",
    "state": "not a field parameter -- did you mean string=?",
    "hhelp": "typo -- did you mean help=?",
}

# Model methods deleted from the ORM. The call still parses, so the only
# symptom is an AttributeError the first time the code path is exercised.
REMOVED_MODEL_METHODS = {
    ("ir.sequence", "get"): "removed in Odoo 9 -- use next_by_code()",
    ("res.company", "_company_default_get"): "deprecated -- use self.env.company",
}

# Callables that must not be invoked at import time in a `default=`.
# `default=fields.Date.today()` freezes to the moment the worker booted.
FROZEN_DEFAULT_CALLS = {"today", "now", "context_today"}

SKIP_DIRS = {".git", "__pycache__", "static", "i18n", "doc", "tools"}


class Finding:
    __slots__ = ("path", "line", "code", "message")

    def __init__(self, path, line, code, message):
        self.path = path
        self.line = line
        self.code = code
        self.message = message

    def __str__(self):
        return f"{self.path}:{self.line}: [{self.code}] {self.message}"

    def sort_key(self):
        return (self.code, self.path, self.line)


def iter_files(root, modules, suffix):
    """Yield every file with `suffix` under the selected modules."""
    for module in modules:
        module_dir = os.path.join(root, module)
        for dirpath, dirnames, filenames in os.walk(module_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(suffix):
                    yield module, os.path.join(dirpath, name)


def read_text(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


# --------------------------------------------------------------------------
# Python checks
# --------------------------------------------------------------------------

def _is_fields_call(node):
    """True for `fields.Something(...)`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "fields"
    )


def _decorator_is(node, *attrs):
    """True when the decorator is `api.<attr>` or a bare `<attr>`."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr in attrs
    if isinstance(target, ast.Name):
        return target.id in attrs
    return False


def _kwarg(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def check_python(root, path, module, findings):
    source = read_text(path)
    relpath = rel(root, path)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        findings.append(Finding(relpath, exc.lineno or 0, "SYNTAX",
                                f"file does not parse: {exc.msg}"))
        return set()

    defined_methods = set()

    for node in ast.walk(tree):
        # -- routes ----------------------------------------------------
        if isinstance(node, ast.Call) and _decorator_is(node, "route"):
            keyword = _kwarg(node, "type")
            if keyword is not None:
                value = _const_str(keyword.value)
                if value is not None and value not in VALID_ROUTE_TYPES:
                    findings.append(Finding(
                        relpath, node.lineno, "ROUTE-TYPE",
                        f"@http.route(type={value!r}) is not a valid Odoo 18 "
                        f"dispatcher; the module will fail to load. "
                        f"Valid: {sorted(VALID_ROUTE_TYPES)}"))

        # -- field kwargs ----------------------------------------------
        if _is_fields_call(node):
            for keyword in node.keywords:
                if keyword.arg in DEAD_FIELD_KWARGS:
                    findings.append(Finding(
                        relpath, keyword.value.lineno, "FIELD-KWARG",
                        f"fields.{node.func.attr}({keyword.arg}=...) -- "
                        f"{DEAD_FIELD_KWARGS[keyword.arg]}"))
            default = _kwarg(node, "default")
            if default is not None and isinstance(default.value, ast.Call):
                func = default.value.func
                attr = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else "")
                if attr in FROZEN_DEFAULT_CALLS:
                    findings.append(Finding(
                        relpath, default.value.lineno, "FROZEN-DEFAULT",
                        f"default={attr}(...) is evaluated once at import "
                        f"time -- every record gets the server boot value. "
                        f"Wrap it: default=lambda self: ..."))

        # -- methods deleted from the ORM ------------------------------
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Subscript)):
            model = _const_str(node.func.value.slice)
            reason = REMOVED_MODEL_METHODS.get((model, node.func.attr))
            if reason:
                findings.append(Finding(
                    relpath, node.lineno, "REMOVED-API",
                    f"env[{model!r}].{node.func.attr}() -- {reason}"))

        # -- 'tree' view mode ------------------------------------------
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _const_str(key) != "view_mode":
                    continue
                mode = _const_str(value)
                if mode and "tree" in mode:
                    findings.append(Finding(
                        relpath, value.lineno, "VIEW-MODE",
                        f"view_mode={mode!r} -- the 'tree' view type was "
                        f"renamed to 'list' in Odoo 18"))

        # -- create() batching -----------------------------------------
        if isinstance(node, ast.FunctionDef):
            defined_methods.add(node.name)
            if node.name == "create":
                for decorator in node.decorator_list:
                    if _decorator_is(decorator, "model"):
                        findings.append(Finding(
                            relpath, node.lineno, "CREATE-BATCH",
                            "@api.model on create() degrades to "
                            "model_create_single in Odoo 18 -- use "
                            "@api.model_create_multi with vals_list"))
            # -- compute depending on its own output -------------------
            if node.name.startswith("_compute_"):
                computed = node.name[len("_compute_"):]
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and _decorator_is(decorator, "depends"):
                        for arg in decorator.args:
                            if _const_str(arg) == computed:
                                findings.append(Finding(
                                    relpath, decorator.lineno, "SELF-DEPENDS",
                                    f"@api.depends({computed!r}) on "
                                    f"{node.name}() -- a compute must not "
                                    f"depend on the field it assigns"))

    # -- duplicate labels within one model -----------------------------
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        labels = defaultdict(list)
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or not _is_fields_call(stmt.value):
                continue
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                continue
            keyword = _kwarg(stmt.value, "string")
            label = _const_str(keyword.value) if keyword is not None else None
            if label:
                labels[label].append((stmt.targets[0].id, stmt.lineno))
        for label, entries in sorted(labels.items()):
            if len(entries) > 1:
                names = ", ".join(name for name, _ in entries)
                findings.append(Finding(
                    relpath, entries[1][1], "DUP-LABEL",
                    f"fields ({names}) on {node.name} share the label "
                    f"{label!r} -- Odoo logs 'have the same label'"))

    return defined_methods


# --------------------------------------------------------------------------
# XML checks
# --------------------------------------------------------------------------

MODEL_CALL_RE = re.compile(r"\bmodel\.(\w+)\s*\(")


def check_xml(root, path, findings):
    relpath = rel(root, path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        findings.append(Finding(relpath, 0, "XML-PARSE",
                                f"file does not parse: {exc}"))
        return [], []

    record_ids = []
    server_calls = []
    for record in tree.iter("record"):
        rec_id = record.get("id")
        if rec_id:
            record_ids.append((rec_id, relpath))
        if record.get("model") not in ("ir.cron", "ir.actions.server"):
            continue
        for field in record.iter("field"):
            if field.get("name") != "code" or not field.text:
                continue
            for method in MODEL_CALL_RE.findall(field.text):
                server_calls.append((method, relpath, rec_id))
    return record_ids, server_calls


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def discover_modules(root):
    return sorted(
        name for name in os.listdir(root)
        if os.path.isfile(os.path.join(root, name, "__manifest__.py"))
    )


def run(root, modules):
    findings = []
    methods_by_module = defaultdict(set)
    ids_by_module = defaultdict(list)
    calls_by_module = defaultdict(list)

    for module, path in iter_files(root, modules, ".py"):
        methods_by_module[module] |= check_python(root, path, module, findings)

    for module, path in iter_files(root, modules, ".xml"):
        record_ids, server_calls = check_xml(root, path, findings)
        ids_by_module[module].extend(record_ids)
        calls_by_module[module].extend(server_calls)

    # A cron whose code calls a method that nobody defines is exactly the
    # 'hr.resignation has no attribute update_employee_status' failure.
    for module, calls in sorted(calls_by_module.items()):
        for method, relpath, rec_id in calls:
            if method not in methods_by_module[module]:
                findings.append(Finding(
                    relpath, 0, "MISSING-METHOD",
                    f"record {rec_id!r} calls model.{method}() but "
                    f"'{method}' is not defined anywhere in module "
                    f"'{module}' -- the job will fail every time it runs"))

    for module, entries in sorted(ids_by_module.items()):
        seen = defaultdict(list)
        for rec_id, relpath in entries:
            seen[rec_id].append(relpath)
        for rec_id, paths in sorted(seen.items()):
            if len(paths) > 1:
                findings.append(Finding(
                    paths[-1], 0, "DUP-XML-ID",
                    f"record id {rec_id!r} is defined {len(paths)} times in "
                    f"'{module}' ({', '.join(sorted(set(paths)))}) -- the "
                    f"last definition silently wins"))

    return findings


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules = argv[1:] or discover_modules(root)
    findings = run(root, modules)

    if not findings:
        print(f"odoo18_compat_check: OK -- {len(modules)} modules, no findings")
        return 0

    findings.sort(key=Finding.sort_key)
    counts = defaultdict(int)
    for finding in findings:
        counts[finding.code] += 1
        print(finding)
    print("")
    print(f"odoo18_compat_check: {len(findings)} finding(s) across "
          f"{len(modules)} module(s)")
    for code in sorted(counts):
        print(f"  {code:<16} {counts[code]}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
