"""The SAST benchmark corpus (#399, ADR-0006).

A fixed set of small diff samples, one per canonical vuln class, PLUS the
known false-positive shape from PR #391 (logging a PUBLIC config path / SSM
param name — which must NOT be flagged). Each sample is an inline unified-diff
body (the `Hunk.body` shape Elder consumes: `+` added lines), so the runner
feeds it through Elder's real review path without any file I/O.

Ground truth per sample is `(vuln_class, path, is_true_positive)`:
- TRUE-POSITIVE samples each seed exactly one real, exploitable vuln of their
  class — a flag is a recall hit.
- The FALSE-POSITIVE sample seeds a non-exploitable shape — ANY flag on it is
  a precision miss (it is the #391 regression guard).

Keep samples MINIMAL and unambiguous: the benchmark measures detection, not
the model's ability to untangle a large file. Adding a class = one entry here;
the scoring + runner are class-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CorpusSample:
    """One benchmark sample + its ground truth.

    `name` is the stable id used as the scoring/baseline key (so a baseline
    survives reordering). `vuln_class` groups per-class recall. `path` is the
    pseudo-file the diff touches (the model sees it; also the finding-match
    key — a finding on this path counts as a flag for this sample). `diff_body`
    is the `Hunk.body` (unified-diff added lines). `is_true_positive=False`
    marks the FP guard (any flag = precision miss)."""

    name: str
    vuln_class: str
    path: str
    diff_body: str
    is_true_positive: bool


# One TRUE-POSITIVE sample per canonical class (ADR-0006 / PRD #392), then the
# PR #391 FALSE-POSITIVE guard. `path` is unique per sample so a finding maps
# to exactly one sample.
_SAMPLES: tuple[CorpusSample, ...] = (
    CorpusSample(
        name="cleartext_secret_log",
        vuln_class="cleartext-secret-log",
        path="bench/cleartext_secret_log.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def on_login(user, password):\n"
            '+    logging.info("login attempt user=%s password=%s", user, password)\n'
            "+    return authenticate(user, password)"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="hardcoded_credential",
        vuln_class="hardcoded-credential",
        path="bench/hardcoded_credential.py",
        # NOTE: a REALISTIC (non-allowlisted) dummy value. SAST engines + secret
        # scanners allowlist the canonical AWS docs example key
        # (wJalrXUtnFEMI/...EXAMPLEKEY) to avoid docs false-positives, so using
        # it here would measure 0 recall for this class. The vuln is the
        # hardcoded secret regardless of the literal.
        diff_body=(
            "@@ -0,0 +1,2 @@\n"
            '+SERVICE_API_KEY = "k3y-live-7f8a9b2c4d6e1f3a5b7c9d0e2f4a6b8c"\n'
            "+client = ServiceClient(api_key=SERVICE_API_KEY)"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="sql_injection",
        vuln_class="sql-injection",
        path="bench/sql_injection.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def get_user(conn, user_id):\n"
            '+    query = "SELECT * FROM users WHERE id = " + user_id\n'
            "+    return conn.execute(query).fetchall()"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="command_injection",
        vuln_class="command-injection",
        path="bench/command_injection.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def ping(host):\n"
            '+    import os\n'
            '+    return os.system("ping -c 1 " + host)'
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="template_injection",
        vuln_class="template-injection",
        path="bench/template_injection.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+from jinja2 import Template\n"
            "+def render(name):\n"
            '+    return Template("Hello " + name).render()'
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="ssrf",
        vuln_class="ssrf",
        path="bench/ssrf.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def fetch(url):\n"
            "+    import requests\n"
            "+    return requests.get(url).text"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="path_traversal",
        vuln_class="path-traversal",
        path="bench/path_traversal.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def read_file(filename):\n"
            '+    with open("/var/data/" + filename) as f:\n'
            "+        return f.read()"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="unsafe_deserialization",
        vuln_class="unsafe-deserialization",
        path="bench/unsafe_deserialization.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+import pickle\n"
            "+def load(blob):\n"
            "+    return pickle.loads(blob)"
        ),
        is_true_positive=True,
    ),
    CorpusSample(
        name="weak_crypto",
        vuln_class="weak-crypto",
        path="bench/weak_crypto.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+import hashlib\n"
            "+def store_password(pw):\n"
            "+    return hashlib.md5(pw.encode()).hexdigest()"
        ),
        is_true_positive=True,
    ),
    # PR #391 FALSE-POSITIVE guard: logs an SSM PARAM NAME (a config path that
    # is already public in the k8s manifest), never a secret VALUE. A correct
    # reviewer suppresses this with a reason; ANY flag here is a precision miss.
    CorpusSample(
        name="fp_public_config_path_log",
        vuln_class="benign-config-log",
        path="bench/fp_public_config_path_log.py",
        diff_body=(
            "@@ -0,0 +1,3 @@\n"
            "+def load_secret(ssm_param_name):\n"
            '+    logging.info("loading secret from SSM param %s", ssm_param_name)\n'
            "+    return ssm.get_parameter(Name=ssm_param_name, WithDecryption=True)"
        ),
        is_true_positive=False,
    ),
)


def load_corpus() -> tuple[CorpusSample, ...]:
    """Return the full corpus. Validates uniqueness invariants the scoring +
    baseline keys rely on, so a malformed corpus fails loudly at load, not as
    a silently-wrong metric."""
    names = [s.name for s in _SAMPLES]
    paths = [s.path for s in _SAMPLES]
    if len(set(names)) != len(names):
        raise ValueError("corpus sample names must be unique (scoring key)")
    if len(set(paths)) != len(paths):
        raise ValueError("corpus sample paths must be unique (finding-match key)")
    if not any(s.is_true_positive for s in _SAMPLES):
        raise ValueError("corpus has no true-positive samples (recall undefined)")
    if not any(not s.is_true_positive for s in _SAMPLES):
        raise ValueError("corpus has no false-positive guard (precision undefined)")
    return _SAMPLES
