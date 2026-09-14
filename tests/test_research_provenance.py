import subprocess

import pytest

from kalshi_predictor.crypto.research_provenance import freeze_code, verify_unchanged


def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "model.py").write_text("probability = 0.5\n")
    subprocess.run(["git", "add", "model.py"], cwd=repo, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "Freeze model",
    ], cwd=repo, check=True, capture_output=True)
    return repo


def test_copies_committed_code_and_detects_later_changes(tmp_path):
    repo = repository(tmp_path)
    proof = freeze_code(repo, tmp_path / "proof", ("model.py",))
    assert (tmp_path / "proof/model.py").read_bytes() == (repo / "model.py").read_bytes()
    assert not proof["release_certified"]
    verify_unchanged(repo, proof)
    (repo / "model.py").write_text("probability = 0.9\n")
    with pytest.raises(ValueError, match="CHANGED_DURING"):
        verify_unchanged(repo, proof)


def test_refuses_dirty_model_before_capture(tmp_path):
    repo = repository(tmp_path)
    (repo / "model.py").write_text("probability = 0.9\n")
    with pytest.raises(ValueError, match="UNCOMMITTED"):
        freeze_code(repo, tmp_path / "proof", ("model.py",))
    assert not (tmp_path / "proof").exists()
