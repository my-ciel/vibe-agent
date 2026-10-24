"""화면 확인용 데모: 가짜 LLM으로 앱을 띄운다. (streamlit run tests/demo_app.py)"""
import os
import runpy
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.fakes import ScriptedLLM, call  # noqa: E402

if "llm_factory" not in st.session_state:
    llm = ScriptedLLM(
        {"text": "먼저 폴더에 뭐가 있는지 볼게요.\n", "calls": [call("c1", "list_dir")]},
        {"text": "비어 있네요. 간단한 페이지를 만들겠습니다.\n",
         "calls": [call("c2", "write_file", path="index.html",
                        content="<!doctype html>\n<html>\n<body>\n  <h1>할 일 목록</h1>\n</body>\n</html>\n")]},
        {"text": "`index.html`을 만들었어요. 브라우저에서 열어 보세요."},
    )
    st.session_state["llm_factory"] = lambda key: llm
    st.session_state["api_key"] = "sk-or-demo"
    st.session_state["root_text"] = os.environ["DEMO_ROOT"]

runpy.run_path(str(ROOT / "app.py"))
