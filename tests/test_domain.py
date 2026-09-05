import pytest
from pydantic import ValidationError

from noveltool.domain import ProjectConfig, ProjectMeta, utc_now


def test_defaults():
    config = ProjectConfig()
    assert (config.min_chars, config.max_chars, config.candidate_count) == (600, 1000, 4)
    assert config.context_window == 20000
    assert config.autosave_seconds == 60
    assert config.writer_model == ""


@pytest.mark.parametrize("change", [
    {"min_chars": 0}, {"max_chars": 599}, {"candidate_count": 0}, {"candidate_count": 33},
    {"context_window": 2047}, {"context_safety_ratio": 0}, {"context_safety_ratio": 1.01},
    {"context_safety_ratio": float("nan")}, {"writer_temperature": float("inf")},
    {"analysis_temperature": -0.1}, {"autosave_seconds": 9}, {"api_timeout_seconds": 0},
    {"min_chars": "600"}, {"min_chars": True}, {"retain_llm_logs": "true"},
    {"api_key": "must-not-be-stored"}, {"api_key_env": "NOT-VALID"},
    {"writer_model": "x" * 257},
])
def test_config_validation(change):
    with pytest.raises(ValidationError):
        ProjectConfig(**change)


@pytest.mark.parametrize("url", [
    "file:///tmp/secret", "http://", "http://name:password@localhost/v1",
    "http://localhost:99999/v1", "http://localhost:0/v1",
    "http://localhost/v1?api_key=secret", "http://local host/v1", "https://x/v1#secret",
])
def test_bad_base_url(url):
    with pytest.raises(ValidationError):
        ProjectConfig(api_base_url=url)


def test_normalization_and_frozen_values():
    config = ProjectConfig(api_base_url=" http://localhost:8000/v1/ ", writer_model=" writer ")
    assert config.api_base_url == "http://localhost:8000/v1"
    assert config.writer_model == "writer"
    with pytest.raises(ValidationError):
        config.min_chars = 1


def test_meta_validates_title_and_time():
    values = {"id": "a" * 32, "title": "测试", "created_at": utc_now(), "updated_at": utc_now(), "saved_at": utc_now()}
    assert ProjectMeta(**values).title == "测试"
    with pytest.raises(ValidationError):
        ProjectMeta(**{**values, "title": "  "})
    with pytest.raises(ValidationError):
        ProjectMeta(**{**values, "updated_at": "2026-09-05T00:00:00"})
