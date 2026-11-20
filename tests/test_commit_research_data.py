"""commit_research_data.sh must commit only the pathspecs it was given.

2026-11-21 docs-site writers audit: the script staged only its own pathspecs
(`git add -- "$@"`) but then ran a bare `git commit`, which commits the WHOLE
index -- so a file another session had staged in ~/docs-site rode along in a
"chore(data): research data refresh" commit and was pushed. The "nothing to
commit" check had the same gap: another session's staged file made it think
there was something to commit.

Runs the real script against a throwaway docs-site clone (HOME is redirected;
the remote is a local bare repo, so nothing leaves the machine).
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "commit_research_data.sh"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _setup(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    home = tmp_path / "home"
    repo = home / "docs-site"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "data").mkdir()
    (repo / "data" / "a-snapshot.json").write_text("{}\n")
    (repo / "pages").mkdir()
    (repo / "pages" / "index.html").write_text("<p>old</p>\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    return home, repo, remote


def _run(home, *pathspecs):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = str(home)
    return subprocess.run(["bash", str(SCRIPT), *pathspecs], env=env,
                          capture_output=True, text=True)


def _other_session_stages_a_page(repo):
    (repo / "pages" / "index.html").write_text("<p>someone else's work</p>\n")
    _git(repo, "add", "pages/index.html")


def test_another_sessions_staged_file_is_not_committed(tmp_path):
    home, repo, remote = _setup(tmp_path)
    _other_session_stages_a_page(repo)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 2}\n')

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["data/a-snapshot.json"]
    # the other session's file is still staged, untouched, and was not pushed
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["pages/index.html"]
    pushed = _git(remote, "show", "--name-only", "--format=", "main").split()
    assert pushed == ["data/a-snapshot.json"]


def test_only_someone_elses_staged_change_means_nothing_to_commit(tmp_path):
    home, repo, _ = _setup(tmp_path)
    _other_session_stages_a_page(repo)
    head = _git(repo, "rev-parse", "HEAD")

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to commit" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["pages/index.html"]


def test_own_change_alone_is_committed_and_pushed(tmp_path):
    home, repo, remote = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 3}\n')

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "committed + pushed" in r.stdout
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")


def test_a_pathspec_that_matches_nothing_does_not_fail_the_commit(tmp_path):
    home, repo, remote = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 4}\n')

    r = _run(home, "data/*-snapshot.json", "data/*-ah-history.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "committed + pushed" in r.stdout
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["data/a-snapshot.json"]
