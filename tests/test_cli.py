from pathlib import Path

import pytest

from noveltool.__main__ import build_parser, main
from noveltool.db import ProjectStore


def test_default_local_single_project():
    args = build_parser().parse_args(["--project", "project.sqlite3"])
    assert args.project == Path("project.sqlite3")
    assert args.host == "127.0.0.1"
    assert args.port == 8765


@pytest.mark.parametrize("argv", [
    [], ["--project", "p", "--port", "0"], ["--project", "p", "--port", "no"],
    ["--project", "p", "--host", "0.0.0.0"],
])
def test_parser_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_version_works_without_project(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_init_only(tmp_path):
    path = tmp_path / "new.sqlite3"
    assert main(["--project", str(path), "--create", "--title", "首次创建", "--init-only"]) == 0
    with ProjectStore.open(path) as store:
        assert store.load().meta.title == "首次创建"


def test_init_never_overwrites(project_path):
    with pytest.raises(SystemExit) as exc:
        main(["--project", str(project_path), "--create", "--init-only"])
    assert exc.value.code == 2


def test_title_and_init_require_create(project_path):
    for extra in (["--title", "误改"], ["--init-only"]):
        with pytest.raises(SystemExit) as exc:
            main(["--project", str(project_path), *extra])
        assert exc.value.code == 2


def test_run_single_process(project_path, monkeypatch):
    called = {}
    monkeypatch.setattr("noveltool.__main__.uvicorn.run", lambda app, **kwargs: called.update(kwargs))
    assert main(["--project", str(project_path)]) == 0
    assert called == {"host": "127.0.0.1", "port": 8765, "workers": 1, "reload": False}
