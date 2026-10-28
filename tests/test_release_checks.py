"""A release must wait for validation, and new typing errors must fail that gate."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml

from tools.check_types import main


def test_both_publication_jobs_require_validation():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    for job in ("docker", "github-release"):
        assert workflow["jobs"][job]["needs"] == "validate"
    assert workflow["jobs"]["validate"]["uses"] == "./.github/workflows/validate.yml"


def test_existing_error_is_accepted_but_a_new_occurrence_is_rejected():
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "tools/mypy-baseline.json").read_text())
    known = next(iter(baseline))
    lines = "\n".join(
        known.replace(": error:", f":{10 + i}: error:") for i in range(baseline[known])
    )
    with patch(
        "tools.check_types.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, lines, ""),
    ):
        assert main() == 0
    lines += "\nqobuz_proxy/app.py:12: error: A new error [test-error]"
    with patch(
        "tools.check_types.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, lines, ""),
    ):
        assert main() == 1


def test_a_broken_type_checker_cannot_look_like_success():
    with patch(
        "tools.check_types.subprocess.run",
        return_value=subprocess.CompletedProcess([], 2, "", "mypy failed"),
    ):
        assert main() == 1
    with patch(
        "tools.check_types.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, "No files could be checked", ""),
    ):
        assert main() == 1
