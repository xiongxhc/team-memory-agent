import os
import subprocess
from pathlib import Path

import pytest


SCANNER = Path(__file__).parents[1] / "scripts" / "check-public.sh"
OPERATOR_DOCS = (
    "README.md",
    "docs/deployment.md",
    "docs/architecture.md",
    "docs/privacy.md",
)
STALE_SCHEDULE_CLAIMS = (
    "The package has no built-in schedule.",
    "The hub has no schedule installation.",
    "TeamMem does not provide hub scheduling.",
    "Built-in hub schedule installation will come later.",
    "Hub schedule installation belongs to an external scheduler.",
)
LEGITIMATE_SCHEDULE_BOUNDARIES = (
    "Package installation alone creates no schedule.",
    "run-daily does not install a schedule.",
    (
        "On a network home, use an external scheduler to invoke "
        "teammem --env-file /absolute/path/to/hub.env run-daily."
    ),
)


def _tracked_repo(tmp_path, name, content):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
    )
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", name],
        check=True,
    )


def test_public_scan_checks_tracked_filenames_with_spaces(tmp_path):
    secret_key = "api_" + "token"
    _tracked_repo(
        tmp_path,
        "private notes.json",
        '{"' + secret_key + '":"not-a-real-secret-123456789"}\n',
    )

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1


def test_public_scan_rejects_non_10_private_network_ranges(tmp_path):
    private_address = "192." + "168.25.4"
    _tracked_repo(tmp_path, "config.txt", f"service={private_address}\n")

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1


def test_public_scan_applies_operator_private_identifier_regex(tmp_path):
    private_name = "Legacy " + "Workspace"
    _tracked_repo(tmp_path, "about.txt", f"{private_name}\n")
    env = dict(os.environ)
    env["TEAMMEM_PUBLIC_DENY_REGEX"] = "Legacy[[:space:]]+Workspace"

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1


def test_public_scan_rejects_tracked_hub_environment_file(tmp_path):
    _tracked_repo(tmp_path, "hub.env", "TEAMMEM_SINCE_DAYS=7\n")

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1


def test_public_scan_rejects_tracked_memberkit_environment_file_at_any_depth(
    tmp_path,
):
    _tracked_repo(
        tmp_path,
        "member/config/memberkit.env",
        "MEMBERKIT_MEMBER=alex\n",
    )

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1


def test_public_scan_ignores_obsolete_schedule_claim_in_historical_plan(
    tmp_path,
):
    _tracked_repo(
        tmp_path,
        "docs/superpowers/plans/historical.md",
        "The package does not yet provide hub schedule installation.\n",
    )

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "operator_doc",
    OPERATOR_DOCS,
)
@pytest.mark.parametrize(
    "claim",
    STALE_SCHEDULE_CLAIMS,
)
def test_public_scan_rejects_obsolete_schedule_claim_in_operator_docs(
    tmp_path,
    operator_doc,
    claim,
):
    _tracked_repo(
        tmp_path,
        operator_doc,
        f"{claim}\n",
    )

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "obsolete hub-scheduling claim found" in result.stdout


@pytest.mark.parametrize(
    "operator_doc",
    OPERATOR_DOCS,
)
@pytest.mark.parametrize(
    "boundary",
    LEGITIMATE_SCHEDULE_BOUNDARIES,
)
def test_public_scan_allows_legitimate_schedule_boundary_in_operator_docs(
    tmp_path,
    operator_doc,
    boundary,
):
    _tracked_repo(tmp_path, operator_doc, f"{boundary}\n")

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "credential",
    [
        "ghp_" + "A" * 36,
        "glpat-" + "A" * 20,
        "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "A" * 24,
    ],
)
def test_public_scan_rejects_provider_token_shapes_in_prose(tmp_path, credential):
    _tracked_repo(tmp_path, "notes.txt", f"temporary credential: {credential}\n")

    result = subprocess.run(
        [str(SCANNER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
