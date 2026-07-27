import os
import subprocess
from pathlib import Path


SCANNER = Path(__file__).parents[1] / "scripts" / "check-public.sh"


def _tracked_repo(tmp_path, name, content):
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
    )
    path = tmp_path / name
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
