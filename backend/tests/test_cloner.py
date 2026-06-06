"""
Unit tests for the cloner's URL validation.

These tests cover all the local validation rules that run *before* any network
call is made.  The actual git clone (network + Docker daemon) is integration-
tested in Phase 8.
"""

import pytest

from app.agent.cloner import InvalidRepoURLError, validate_url


# ── Happy path ────────────────────────────────────────────────────────────────


def test_valid_owner_repo():
    owner, repo = validate_url("https://github.com/pallets/flask")
    assert owner == "pallets"
    assert repo == "flask"


def test_valid_with_git_suffix():
    owner, repo = validate_url("https://github.com/pallets/flask.git")
    assert owner == "pallets"
    assert repo == "flask"  # .git stripped by the regex non-greedy match


def test_valid_dotted_repo_name():
    # Repo names can contain dots (e.g. "my.project")
    owner, repo = validate_url("https://github.com/user/my.project")
    assert repo == "my.project"


def test_valid_hyphenated():
    owner, repo = validate_url("https://github.com/docker-library/postgres")
    assert owner == "docker-library"
    assert repo == "postgres"


def test_valid_underscore():
    owner, repo = validate_url("https://github.com/django/django_rest_framework")
    assert owner == "django"
    assert repo == "django_rest_framework"


# ── Scheme checks (SSRF via HTTP, non-URL inputs) ────────────────────────────


def test_rejects_http():
    with pytest.raises(InvalidRepoURLError, match="HTTPS"):
        validate_url("http://github.com/owner/repo")


def test_rejects_ftp():
    with pytest.raises(InvalidRepoURLError, match="HTTPS"):
        validate_url("ftp://github.com/owner/repo")


def test_rejects_no_scheme():
    with pytest.raises(InvalidRepoURLError):
        validate_url("github.com/owner/repo")


# ── Host checks (SSRF — reject non-github, private IPs, localhost) ────────────


def test_rejects_gitlab():
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://gitlab.com/owner/repo")


def test_rejects_bitbucket():
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://bitbucket.org/owner/repo")


def test_rejects_localhost():
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://localhost/owner/repo")


def test_rejects_loopback_ip():
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://127.0.0.1/owner/repo")


def test_rejects_private_ip():
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://192.168.1.1/owner/repo")


def test_rejects_metadata_ip():
    # AWS/GCP instance metadata endpoint — classic SSRF target
    with pytest.raises(InvalidRepoURLError, match="github.com"):
        validate_url("https://169.254.169.254/latest/meta-data/")


def test_rejects_explicit_port():
    with pytest.raises(InvalidRepoURLError, match="port"):
        validate_url("https://github.com:8080/owner/repo")


# ── Path checks ───────────────────────────────────────────────────────────────


def test_rejects_bare_domain():
    with pytest.raises(InvalidRepoURLError):
        validate_url("https://github.com/")


def test_rejects_owner_only():
    with pytest.raises(InvalidRepoURLError):
        validate_url("https://github.com/owner")


def test_rejects_deep_path():
    # We only accept /owner/repo — not /owner/repo/tree/main/subdir
    with pytest.raises(InvalidRepoURLError):
        validate_url("https://github.com/owner/repo/tree/main/src")
