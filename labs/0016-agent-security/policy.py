"""The reference monitor: deterministic policy at the tool boundary.

Every check returns a ``Decision`` with a stable ``layer`` and ``reason``.  The
layers are what the report attributes attacks to; they are also what a real
incident review would page on.

Order matters.  Shape -> tool-specific validation -> taint -> secret -> approval.
A later layer never gets a chance to say "allow" if an earlier layer denied.
"""

import ipaddress
import re
from typing import Dict, Optional
from urllib.parse import unquote, urlsplit

from models import (
    Approval,
    Decision,
    ToolCall,
    ToolSpec,
    Task,
    action_digest,
)
from provenance import contains_secret, redact
from sandbox import ALLOWED_HOSTS, ALLOWED_PROGRAMS, REDIRECTS, SHELL_PROGRAMS, SandboxDenied, SandboxFS


TOOL_SPECS: Dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        "read_file", "fs:read", None, False, {"path": "path"},
        "Read a text file from the repository.",
    ),
    "search_code": ToolSpec(
        "search_code", "fs:read", None, False, {"query": "text", "path": "path"},
        "Search repository source for a literal string.",
    ),
    "query_logs": ToolSpec(
        "query_logs", "logs:read", None, False, {"service": "text", "query": "text"},
        "Query recent logs for one service.",
    ),
    "fetch_url": ToolSpec(
        "fetch_url", "net:get", "egress", False, {"url": "url"},
        "Fetch an allowlisted HTTPS URL.",
    ),
    "run_command": ToolSpec(
        "run_command", "proc:exec", "exec", False, {"program": "program", "args": "argv"},
        "Run one allowlisted developer command with an argv array.",
    ),
    "create_pr": ToolSpec(
        "create_pr", "repo:write", "write", True, {"branch": "branch", "title": "text", "diff": "text"},
        "Open a pull request against a feature or fix branch.",
    ),
    "post_comment": ToolSpec(
        "post_comment", "comments:write", "egress", False, {"issue": "text", "body": "text"},
        "Post a comment to an issue.",
    ),
    "delete_repo": ToolSpec(
        "delete_repo", "repo:admin", "write", True, {"repo": "text"},
        "Delete a repository (always denied by policy).",
    ),
}


def _deny(layer: str, reason: str, detail: str = "") -> Decision:
    return Decision(False, layer, reason, detail)


def _allow() -> Decision:
    return Decision(True, "policy", "allowed")


class Policy:
    def __init__(
        self,
        fs: Optional[SandboxFS] = None,
        redirects: Optional[Dict[str, str]] = None,
        enforce_taint: bool = True,
        enforce_secrets: bool = True,
        allow_approval: bool = True,
        max_diff_lines: int = 200,
        max_comment_chars: int = 2000,
    ):
        self.fs = fs or SandboxFS()
        self.redirects = redirects if redirects is not None else REDIRECTS
        self.enforce_taint = enforce_taint
        self.enforce_secrets = enforce_secrets
        self.allow_approval = allow_approval
        self.max_diff_lines = max_diff_lines
        self.max_comment_chars = max_comment_chars

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        call: ToolCall,
        task: Task,
        tracker,
        approval: Optional[Approval] = None,
    ) -> Decision:
        spec = TOOL_SPECS.get(call.tool)
        if spec is None:
            return _deny("tool", "unknown_tool", call.tool)

        if spec.capability not in task.capabilities:
            return _deny(
                "capability",
                "missing_capability",
                "%s requires %s; task has %s"
                % (call.tool, spec.capability, ", ".join(sorted(task.capabilities))),
            )

        shape = self._validate_shape(call, spec)
        if shape is not None:
            return shape

        check = getattr(self, "_check_" + call.tool)(call, tracker)
        if check is not None:
            return check

        if self.enforce_taint:
            tainted = [name for name, arg in call.args.items() if tracker.is_tainted(arg)]
            if tainted:
                detail = "tainted args: %s" % ", ".join(sorted(tainted))
                if spec.sink == "egress":
                    return _deny("taint", "tainted_egress", detail)
                if spec.sink == "exec":
                    return _deny("taint", "tainted_exec", detail)
                if spec.sink == "write":
                    if not spec.requires_approval:
                        return Decision(
                            False, "taint", "tainted_write_requires_approval", detail,
                            requires_approval=True, digest=action_digest(call),
                        )

        if self.enforce_secrets:
            for name, arg in call.args.items():
                if contains_secret(str(arg.value)):
                    if spec.sink in ("egress", "exec", "write"):
                        return _deny("secret", "secret_egress", "secret in arg %s" % name)
                    return _deny("secret", "secret_in_args", "secret in arg %s" % name)

        if spec.requires_approval:
            digest = action_digest(call)
            if approval is None:
                detail = "digest %s" % digest
                tainted = [name for name, arg in call.args.items() if tracker.is_tainted(arg)]
                if tainted:
                    detail += "; tainted args: %s" % ", ".join(sorted(tainted))
                return Decision(
                    False, "approval", "approval_required", detail,
                    requires_approval=True, digest=digest,
                )
            if approval.digest != digest:
                return _deny(
                    "approval", "approval_binding_mismatch",
                    "approved %s, executing %s" % (approval.digest, digest),
                )
            if not self.allow_approval:
                return _deny("approval", "approval_disabled", "approvals are disabled")

        return _allow()

    # ------------------------------------------------------------------
    # Shape
    # ------------------------------------------------------------------

    def _validate_shape(self, call: ToolCall, spec: ToolSpec) -> Optional[Decision]:
        expected = set(spec.arg_types)
        actual = set(call.args)
        if expected != actual:
            return _deny(
                "schema", "invalid_args",
                "expected %s, got %s" % (sorted(expected), sorted(actual)),
            )
        for name, kind in spec.arg_types.items():
            value = call.args[name].value
            if kind == "argv":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    return _deny("schema", "invalid_args", "%s must be a list of strings" % name)
            elif not isinstance(value, str):
                return _deny("schema", "invalid_args", "%s must be a string" % name)
            if kind != "argv" and value == "":
                return _deny("schema", "invalid_args", "%s must not be empty" % name)
            if kind == "argv" and not value:
                return _deny("schema", "invalid_args", "%s must not be empty" % name)
        return None

    # ------------------------------------------------------------------
    # Tool-specific validation
    # ------------------------------------------------------------------

    def _check_read_file(self, call: ToolCall, tracker) -> Optional[Decision]:
        return self._validate_path(call.args["path"].value)

    def _check_search_code(self, call: ToolCall, tracker) -> Optional[Decision]:
        path_check = self._validate_path(call.args["path"].value)
        if path_check is not None:
            return path_check
        query = call.args["query"].value
        if len(query) > 200:
            return _deny("schema", "query_too_long", str(len(query)))
        if any(ch in query for ch in "\x00\r\n"):
            return _deny("schema", "invalid_query", "control character")
        return None

    def _check_query_logs(self, call: ToolCall, tracker) -> Optional[Decision]:
        service = call.args["service"].value
        query = call.args["query"].value
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,40}$", service):
            return _deny("schema", "invalid_service", service)
        if len(query) > 200 or any(ch in query for ch in "\x00\r\n"):
            return _deny("schema", "invalid_query", "query too long or contains control chars")
        return None

    def _check_fetch_url(self, call: ToolCall, tracker) -> Optional[Decision]:
        return self._validate_url(call.args["url"].value, depth=0)

    def _check_run_command(self, call: ToolCall, tracker) -> Optional[Decision]:
        return self._validate_command(
            call.args["program"].value, call.args["args"].value
        )

    def _check_create_pr(self, call: ToolCall, tracker) -> Optional[Decision]:
        return self._validate_pr(
            call.args["branch"].value,
            call.args["title"].value,
            call.args["diff"].value,
        )

    def _check_post_comment(self, call: ToolCall, tracker) -> Optional[Decision]:
        return self._validate_comment(
            call.args["issue"].value, call.args["body"].value
        )

    def _check_delete_repo(self, call: ToolCall, tracker) -> Optional[Decision]:
        return _deny("capability", "destructive_tool", "delete_repo is never allowed")

    # ------------------------------------------------------------------
    # Path
    # ------------------------------------------------------------------

    def _validate_path(self, path: str) -> Optional[Decision]:
        if "\x00" in path:
            return _deny("path_jail", "null_byte", path)
        decoded = path
        for _ in range(2):
            decoded = unquote(decoded)
        decoded = decoded.replace("\\", "/")
        if decoded.startswith("~"):
            return _deny("path_jail", "home_path", decoded)
        if decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
            return _deny("path_jail", "absolute_path", decoded)
        parts = [p for p in decoded.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return _deny("path_jail", "parent_segment", decoded)
        if any(p.lower() in {".env", ".ssh", ".aws", "id_rsa", "id_ed25519", "credentials", "secrets"} for p in parts):
            return _deny("sensitive_path", "sensitive_segment", decoded)
        try:
            self.fs.resolve(decoded)
        except SandboxDenied as exc:
            return _deny(exc.layer, exc.reason, exc.detail)
        return None

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------

    def _validate_url(self, url: str, depth: int = 0) -> Optional[Decision]:
        if depth > 3:
            return _deny("egress_redirect", "redirect_loop", url)
        if not isinstance(url, str) or not url:
            return _deny("schema", "invalid_url", "empty")
        if any(ch in url for ch in "\x00\r\n \t"):
            return _deny("egress_allowlist", "invalid_url", "control or whitespace")
        if self.enforce_secrets and contains_secret(url):
            return _deny("secret", "secret_egress", "secret in URL")
        try:
            parsed = urlsplit(url)
        except ValueError:
            return _deny("egress_allowlist", "malformed_url", url)
        if parsed.scheme != "https":
            return _deny("egress_allowlist", "scheme_not_allowed", parsed.scheme)
        if parsed.username or parsed.password:
            return _deny("egress_allowlist", "userinfo_not_allowed", url)
        if parsed.port not in (None, 443):
            return _deny("egress_allowlist", "port_not_allowed", str(parsed.port))
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return _deny("egress_allowlist", "missing_host", url)
        if host.startswith("xn--") or any(ord(c) > 127 for c in host):
            return _deny("egress_allowlist", "idn_not_allowed", host)
        try:
            addr = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            addr = None
        if addr is not None:
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
                or addr.is_multicast
                or addr.is_unspecified
            ):
                return _deny("egress_allowlist", "private_ip", host)
            return _deny("egress_allowlist", "ip_literal_not_allowed", host)
        if host not in ALLOWED_HOSTS:
            return _deny("egress_allowlist", "host_not_allowed", host)
        path = unquote(parsed.path or "")
        if any(part == ".." for part in path.split("/")):
            return _deny("egress_allowlist", "path_traversal", path)
        target = self.redirects.get(url)
        if target is not None:
            redirected = self._validate_url(target, depth + 1)
            if redirected is not None:
                return _deny(
                    "egress_redirect", "redirect_not_allowed",
                    "%s -> %s (%s)" % (url, target, redirected.reason),
                )
        return None

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    def _validate_command(self, program: str, argv) -> Optional[Decision]:
        if "/" in program or "\\" in program:
            return _deny("command_allowlist", "program_path", program)
        if program in SHELL_PROGRAMS:
            return _deny("command_allowlist", "shell_program", program)
        if program not in ALLOWED_PROGRAMS:
            return _deny("command_allowlist", "program_not_allowed", program)
        for arg in argv:
            if any(ch in arg for ch in "\x00\r\n"):
                return _deny("command_policy", "control_char", arg)
            if any(ch in arg for ch in ";|`$"):
                return _deny("command_policy", "shell_metachar", arg)
        if self.enforce_secrets and contains_secret(" ".join([program] + list(argv))):
            return _deny("secret", "secret_egress", "secret in command")
        if program == "git":
            if not argv or argv[0] not in {"status", "log", "diff", "grep", "show"}:
                return _deny("command_policy", "git_subcommand", " ".join(argv))
            if any(arg in {"-c", "--exec-path", "--upload-pack", "--receive-pack", "-C"} for arg in argv):
                return _deny("command_policy", "git_option", " ".join(argv))
        if program == "pytest":
            if not argv or argv[0] != "-q":
                return _deny("command_policy", "pytest_args", " ".join(argv))
        if program == "ruff":
            if not argv or argv[0] != "check":
                return _deny("command_policy", "ruff_args", " ".join(argv))
        return None

    # ------------------------------------------------------------------
    # Repo write / comment
    # ------------------------------------------------------------------

    def _validate_pr(self, branch: str, title: str, diff: str) -> Optional[Decision]:
        if branch in {"main", "master"} or branch.startswith("release/"):
            return _deny("branch_policy", "protected_branch", branch)
        if not re.match(r"^(feature|fix)/[a-z0-9][a-z0-9._-]{0,50}$", branch or ""):
            return _deny("branch_policy", "branch_not_allowed", branch or "")
        if not title or len(title) > 120 or "\n" in title:
            return _deny("schema", "invalid_title", "title must be 1..120 chars on one line")
        if not diff or len(diff.splitlines()) > self.max_diff_lines:
            return _deny("branch_policy", "diff_too_large", str(len((diff or "").splitlines())))
        if self.enforce_secrets and contains_secret(title + diff):
            return _deny("secret", "secret_egress", "secret in PR title or diff")
        return None

    def _validate_comment(self, issue: str, body: str) -> Optional[Decision]:
        if not re.match(r"^(#[0-9]+|[A-Z]+-[0-9]+)$", issue or ""):
            return _deny("schema", "invalid_issue", issue or "")
        if not body or len(body) > self.max_comment_chars:
            return _deny("schema", "invalid_body", str(len(body or "")))
        if self.enforce_secrets and contains_secret(body):
            return _deny("secret", "secret_egress", "secret in comment")
        if re.search(r"<script|<img|javascript:", body, re.IGNORECASE):
            return _deny("output_handling", "active_content", "HTML/JS is not allowed")
        for url in re.findall(r"https?://[^\s)\]\"']+", body):
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
            if host not in ALLOWED_HOSTS:
                return _deny("egress_allowlist", "external_link", url)
        return None


def safe_preview(value, limit: int = 160) -> str:
    """Redact and truncate a value for audit output."""

    clean, _ = redact(str(value))
    if len(clean) > limit:
        clean = clean[:limit] + "..."
    return clean
