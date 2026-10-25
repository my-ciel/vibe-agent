"""모든 테스트가 사용자의 실제 홈 폴더(~/.vibe-agent)를 건드리지 않도록 저장 위치를 임시 폴더로 바꾼다."""
import pytest

from vibe_agent import errorlog, models, processes, sessions, settings


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    import streamlit as st

    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings" / "s.json")
    monkeypatch.setattr(sessions, "SESSIONS_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(processes, "REGISTRY_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(errorlog, "LOG_PATH", tmp_path / "errors.log")
    monkeypatch.setattr(models, "fetch_catalog", lambda *a, **k: {})
    st.cache_resource.clear()  # 앱 전체가 공유하는 프로세스 관리자를 테스트마다 새로 만든다
    yield
    st.cache_resource.clear()


