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


def _clean_env():
    """No GIT_* from the caller (GIT_INDEX_FILE, GIT_DIR ...) may point git at a real repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=_clean_env()).stdout


def _names(cwd, *args):
    """File names from a git listing command, NUL-separated so no name is split."""
    out = subprocess.run(["git", *args, "-z"], cwd=cwd, check=True,
                         capture_output=True, env=_clean_env()).stdout.decode()
    return sorted(n for n in out.split("\0") if n)


def _tree(cwd, rev):
    return _names(cwd, "ls-tree", "-r", "--name-only", rev)


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
    env = _clean_env()
    env["HOME"] = str(home)
    return subprocess.run(["bash", str(SCRIPT), *pathspecs], env=env,
                          capture_output=True, text=True)


def _other_session_stages_a_page(repo):
    (repo / "pages" / "index.html").write_text("<p>someone else's work</p>\n")
    _git(repo, "add", "pages/index.html")


def test_another_sessions_staged_file_is_not_committed(tmp_path):
    home, repo, remote = _setup(tmp_path)
    _other_session_stages_a_page(repo)
    staged_blob = _git(repo, "ls-files", "-s", "pages/index.html")
    (repo / "data" / "a-snapshot.json").write_text('{"v": 2}\n')

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _names(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == ["data/a-snapshot.json"]
    # the other session's file is still staged with the same blob, and was not pushed
    assert _names(repo, "diff", "--cached", "--name-only") == ["pages/index.html"]
    assert _git(repo, "ls-files", "-s", "pages/index.html") == staged_blob
    assert _git(remote, "show", "main:pages/index.html") == "<p>old</p>\n"
    assert _git(remote, "show", "main:data/a-snapshot.json") == '{"v": 2}\n'


def test_only_someone_elses_staged_change_means_nothing_to_commit(tmp_path):
    home, repo, _ = _setup(tmp_path)
    _other_session_stages_a_page(repo)
    head = _git(repo, "rev-parse", "HEAD")

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to commit" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _names(repo, "diff", "--cached", "--name-only") == ["pages/index.html"]


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
    assert _names(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == ["data/a-snapshot.json"]


def test_two_valid_pathspecs_and_one_that_matches_nothing(tmp_path):
    home, repo, remote = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 5}\n')
    (repo / "data" / "a-news.json").write_text("[]\n")

    r = _run(home, "data/*-snapshot.json", "data/*-ah-history.json", "data/*-news.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "no file matches [data/*-ah-history.json], skipped" in r.stdout
    assert _tree(remote, "main") == ["data/a-news.json", "data/a-snapshot.json", "pages/index.html"]


def test_a_rename_inside_our_glob_commits_the_deletion_too(tmp_path):
    home, repo, remote = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").rename(repo / "data" / "b-snapshot.json")

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _tree(remote, "main") == ["data/b-snapshot.json", "pages/index.html"]
    assert _names(repo, "diff", "--cached", "--name-only") == []


def test_file_names_with_spaces_tabs_and_chinese_survive(tmp_path):
    home, repo, remote = _setup(tmp_path)
    odd = ["data/空 格-snapshot.json", "data/a\tb-snapshot.json"]
    for name in odd:
        (repo / name).write_text("{}\n")

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    tree = _tree(remote, "main")
    for name in odd:
        assert name in tree


def test_a_listed_name_is_taken_literally_not_as_a_pattern(tmp_path):
    # x[1]-snapshot.json as a glob also matches x1-snapshot.json, which someone else
    # changed but which is outside the pathspec we were given.
    home, repo, remote = _setup(tmp_path)
    (repo / "data" / "x1-snapshot.json").write_text("{}\n")
    _git(repo, "add", "data/x1-snapshot.json")
    _git(repo, "commit", "-q", "-m", "x1 exists")
    _git(repo, "push", "-q", "origin", "main")
    (repo / "data" / "x1-snapshot.json").write_text('{"changed": "by someone"}\n')
    (repo / "data" / "x[1]-snapshot.json").write_text("{}\n")

    r = _run(home, "data/x?1?-snapshot.json")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "data/x[1]-snapshot.json" in _tree(remote, "main")
    assert _git(remote, "show", "main:data/x1-snapshot.json") == "{}\n"


def test_a_magic_pathspec_is_refused_before_the_index_is_touched(tmp_path):
    home, repo, _ = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 6}\n')
    head = _git(repo, "rev-parse", "HEAD")

    r = _run(home, "data/*-snapshot.json", ":(exclude)data/manual-snapshot.json")

    assert r.returncode == 1
    assert "magic pathspec not supported" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _names(repo, "diff", "--cached", "--name-only") == []


def test_a_locked_index_fails_loudly_and_commits_nothing(tmp_path):
    home, repo, _ = _setup(tmp_path)
    (repo / "data" / "a-snapshot.json").write_text('{"v": 7}\n')
    (repo / ".git" / "index.lock").write_text("")
    head = _git(repo, "rev-parse", "HEAD")

    r = _run(home, "data/*-snapshot.json")

    assert r.returncode == 1
    assert "git add failed" in r.stdout
    assert _git(repo, "rev-parse", "HEAD") == head
