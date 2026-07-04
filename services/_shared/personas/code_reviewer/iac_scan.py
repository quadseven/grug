"""Infrastructure-as-code misconfiguration detection for Elder (#447, ADR-0007
Track 1 slice 2 - the IaC half; follows the secret-scan half #436).

Flags insecure IaC a PR INTRODUCES on added lines, producing the SAME
`Candidate` shape the SAST/SCA/secret pipeline uses, so the exploitability judge
(`sast.judge_candidates`) and the publish path are reused unchanged - IaC
scanning is just a new candidate SOURCE.

Why a dedicated source: the vendored SAST ruleset is `languages: [python]`, and
SCA/secret cover deps + credentials - an open security group, a privileged k8s
container, a public S3 ACL, or a root Dockerfile is invisible today. This
detector is diff-scoped (added lines only, like the other Track-1 sources) and
FILE-TYPE-AWARE: Terraform patterns fire only on `*.tf`/`*.tfvars`, k8s/compose
patterns only on `*.yaml`/`*.yml`, Dockerfile patterns only on Dockerfiles; a
few cross-cutting patterns (open CIDR, world-writable, pipe-to-shell) fire on
any IaC file.

Recall-liberal by design (a detector miss is unrecoverable; an over-flag is
recovered by the judge, which can confirm an open `0.0.0.0/0` is a public ALB
by design vs an exposed admin port). The snippet carries the misconfig KIND +
the trimmed line - IaC config is not a secret value, so the line is safe to
publish.

Pure: no IO, no network, no logging - deterministic + unit-tested.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .diff_parser import DiffHunk
from .sast import IAC_MISCONFIG, Candidate

__all__ = ["IAC_MISCONFIG", "scan_iac"]

# Cost bounds, mirroring secret_scan: cap candidates, skip pathological lines,
# and stop once the diff's added text exceeds a total byte budget.
_MAX_FINDINGS = 100
_MAX_LINE_LEN = 4096
_MAX_SCAN_BYTES = 1_048_576

# Published snippet line is trimmed to keep the finding message bounded.
_SNIPPET_MAX = 200


def _is_dockerfile(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return base == "dockerfile" or base.startswith("dockerfile.") or base.endswith(".dockerfile")


def _is_terraform(path: str) -> bool:
    p = path.lower()
    return p.endswith(".tf") or p.endswith(".tfvars")


def _is_yaml(path: str) -> bool:
    p = path.lower()
    return p.endswith(".yaml") or p.endswith(".yml")


def _is_iac(path: str) -> bool:
    return _is_dockerfile(path) or _is_terraform(path) or _is_yaml(path)


@dataclass(frozen=True, slots=True)
class _Rule:
    """A high-signal IaC misconfiguration. `kind` labels it for the snippet,
    `regex` matches the offending construct, `applies` gates it by file type so
    a k8s pattern never fires on Terraform (and vice versa)."""

    kind: str
    regex: re.Pattern[str]
    applies: Callable[[str], bool]


# Each rule is a recognizable insecure construct. Matching one is strong
# evidence; the judge still confirms it is exploitable in context.
_RULES: tuple[_Rule, ...] = (
    # --- cross-cutting (any IaC file) ---
    _Rule("network open to the world (0.0.0.0/0)", re.compile(r"0\.0\.0\.0/0"), _is_iac),
    _Rule("IPv6 network open to the world (::/0)", re.compile(r"(?<![:\w])::/0"), _is_iac),
    _Rule("world-writable permissions (chmod 777)", re.compile(r"chmod\s+(?:-R\s+)?0?777\b"), _is_iac),
    _Rule("pipe-to-shell install", re.compile(r"(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b"), _is_iac),
    # --- Kubernetes / compose YAML ---
    _Rule("privileged container", re.compile(r"(?i)privileged:\s*true\b"), _is_yaml),
    _Rule("host network namespace", re.compile(r"(?i)hostNetwork:\s*true\b"), _is_yaml),
    _Rule("host PID namespace", re.compile(r"(?i)hostPID:\s*true\b"), _is_yaml),
    _Rule("host IPC namespace", re.compile(r"(?i)hostIPC:\s*true\b"), _is_yaml),
    _Rule("privilege escalation allowed", re.compile(r"(?i)allowPrivilegeEscalation:\s*true\b"), _is_yaml),
    _Rule("container may run as root", re.compile(r"(?i)runAsNonRoot:\s*false\b"), _is_yaml),
    _Rule("writable root filesystem", re.compile(r"(?i)readOnlyRootFilesystem:\s*false\b"), _is_yaml),
    _Rule("TLS verification disabled", re.compile(r"(?i)insecure-?skip-?tls-?verify:\s*true\b"), _is_yaml),
    # --- Terraform ---
    _Rule("public object ACL", re.compile(r"""(?i)acl\s*=\s*["']public-read(?:-write)?["']"""), _is_terraform),
    _Rule("resource publicly accessible", re.compile(r"(?i)publicly_accessible\s*=\s*true\b"), _is_terraform),
    _Rule("encryption disabled", re.compile(r"(?i)(?:encrypted|encryption|storage_encrypted)\s*=\s*false\b"), _is_terraform),
    _Rule("deletion protection disabled", re.compile(r"(?i)(?:deletion_protection|enable_deletion_protection)\s*=\s*false\b"), _is_terraform),
    # --- Dockerfile ---
    _Rule("container runs as root (USER root)", re.compile(r"(?im)^\s*USER\s+root\b"), _is_dockerfile),
    _Rule("unpinned :latest base image", re.compile(r"(?im)^\s*FROM\s+\S+:latest\b"), _is_dockerfile),
    _Rule("remote fetch into image (ADD <url>)", re.compile(r"(?im)^\s*ADD\s+https?://"), _is_dockerfile),
)


@dataclass(frozen=True, slots=True)
class _Misconfig:
    """A detected IaC misconfiguration on an added line. `(file, line)` anchor it
    to the diff; `kind` labels it; `snippet` is the trimmed offending line."""

    file: str
    line: int
    kind: str
    snippet: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """[(new_side_line_number, added_text)] for added lines in a hunk. New-side
    number advances on added/context lines, not on removed lines. (Mirrors
    secret_scan's helper - duplicated rather than shared while the rule-of-three
    extraction is deferred, per ADR-0001.)"""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


def _detect(path: str, text: str) -> tuple[str, str] | None:
    """Return `(kind, matched)` for the first rule that applies to this file
    type AND matches the line, else None. `matched` is the exact substring, used
    only for in-memory dedup."""
    for rule in _RULES:
        if not rule.applies(path):
            continue
        m = rule.regex.search(text)
        if m:
            return rule.kind, m.group(0)
    return None


def scan_iac(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """IaC-misconfig candidate source: a Candidate per insecure IaC construct a
    PR introduces, on added lines of Terraform / k8s-compose YAML / Dockerfiles.
    Diff-scoped, file-type-aware, content-deduped by `(file, kind, matched)` (a
    repeated misconfig in a file is reported once - bounds judge cost), capped at
    `_MAX_FINDINGS`. Same `Candidate` shape the SAST pipeline judges + publishes,
    so the judge decides whether the misconfig is actually exploitable (vs an
    intentional public endpoint)."""
    found: list[_Misconfig] = []
    seen: set[tuple[str, str, str]] = set()
    scanned = 0
    for hunk in hunks:
        if not _is_iac(hunk.file_path):
            continue
        for lineno, text in _added_lines(hunk):
            if len(text) > _MAX_LINE_LEN:
                continue
            scanned += len(text)
            if scanned > _MAX_SCAN_BYTES:
                return _to_candidates(found)
            hit = _detect(hunk.file_path, text)
            if hit is None:
                continue
            kind, matched = hit
            key = (hunk.file_path, kind, matched)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                _Misconfig(file=hunk.file_path, line=lineno, kind=kind, snippet=text.strip()[:_SNIPPET_MAX])
            )
            if len(found) >= _MAX_FINDINGS:
                return _to_candidates(found)
    return _to_candidates(found)


def _to_candidates(found: list[_Misconfig]) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            vuln_class=IAC_MISCONFIG,
            file=m.file,
            line=m.line,
            snippet=f"{m.kind}: {m.snippet}",
        )
        for m in found
    )
