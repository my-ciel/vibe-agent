"""Vibe Agent - 채팅으로 진행하는 수업용 코딩 에이전트 (Streamlit UI).

실행:  streamlit run app.py
"""
from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from vibe_agent import models as model_catalog
from vibe_agent import errorlog, sessions
from vibe_agent.attachments import ALLOWED_EXTS, AttachmentView, Upload, prepare_message
from vibe_agent.cost import CostTracker
from vibe_agent.instructions import load_instructions
from vibe_agent.processes import ProcessManager
from vibe_agent.project_rules import find_rule_files
from vibe_agent.folder_picker import pick_folder
from vibe_agent.llm import LLMError, OpenRouterClient, fetch_key_info
from vibe_agent.loop import (
    Agent,
    ApprovalRequest,
    Done,
    ErrorEvent,
    Notice,
    TextDelta,
    ToolResult,
    ToolStart,
    UsageUpdate,
)
from vibe_agent.settings import load_settings, save_settings
from vibe_agent.transcript import ToolEntry, build_turns, tool_title

CUSTOM_MODEL = "__custom__"
BROWSE_MODEL = "__browse__"
EDIT_AUTO_APPROVE = {"write_file", "edit_file"}
DIFF_TOOLS = {"write_file", "edit_file"}

st.set_page_config(page_title="Vibe Agent", page_icon="🤖", layout="wide")
S = st.session_state


# ------------------------------------------------------------------ 상태


def _init_state() -> None:
    saved = load_settings()
    choices = model_catalog.load_model_choices()
    ids = [m["id"] for m in choices]
    S.setdefault("agent", None)
    S.setdefault("agent_root", None)  # 현재 대화가 속한 폴더
    S.setdefault("agent_key", None)
    S.setdefault("session_id", None)
    S.setdefault("session_created", 0.0)
    S.setdefault("saved_sig", None)
    S.setdefault("last_save", 0.0)
    S.setdefault("save_failed", False)
    S.setdefault("pending", None)  # 승인 대기 중인 ApprovalRequest
    S.setdefault("run_requested", False)
    S.setdefault("key_info", None)
    S.setdefault("key_error", None)
    S.setdefault("notice", None)
    S.setdefault("root_text", saved.get("last_root", ""))
    last_model = saved.get("last_model", "")
    S.setdefault("model_choice", last_model if last_model in ids else ids[0])
    S.setdefault("custom_model", last_model if last_model and last_model not in ids else "")


def on_pick_folder() -> None:
    path = pick_folder()
    if path:
        S.root_text = path
    else:
        S.notice = "폴더 선택창을 열 수 없거나 취소되었습니다. 경로를 직접 입력해 주세요."


@st.cache_resource
def _process_manager() -> ProcessManager:
    """앱 전체가 공유하는 백그라운드 프로세스 관리자 (새로고침이나 대화 전환에도 서버가 유지된다)."""
    manager = ProcessManager()
    manager.cleanup_stale()  # 이전 실행이 비정상 종료되어 남긴 프로세스 정리
    return manager


def autosave(force: bool = False) -> None:
    """현재 대화를 저장한다. 바뀐 게 없으면 건너뛰고, 실행 중에는 5초에 한 번만 저장한다."""
    agent = S.get("agent")
    if agent is None or not S.get("save_sessions", True):
        return
    sig = (len(agent.messages), len(agent.decisions), round(agent.tracker.cost_usd, 6))
    now = time.monotonic()
    if sig == S.saved_sig or (not force and now - S.last_save < 5):
        return
    if sessions.save(agent, S.session_id, S.session_created):
        S.saved_sig, S.last_save, S.save_failed = sig, now, False
    else:
        S.save_failed = True


def open_session(root: Path, session_id: str | None = None, new: bool = False) -> None:
    """폴더의 대화를 연다. 지정한 대화, 없으면 가장 최근 대화, new=True면 빈 새 대화."""
    factory = S.get("llm_factory", OpenRouterClient)
    api_key = S.get("api_key", "").strip()
    agent = Agent(
        llm=factory(api_key), model="", root=root, tracker=CostTracker(),
        instructions=load_instructions(), processes=_process_manager(),
    )
    if not new and session_id is None:
        known = sessions.list_sessions(agent.root)
        session_id = known[0].id if known else None
    created = time.time()
    data = sessions.load(agent.root, session_id) if session_id else None
    if data:
        try:
            sessions.restore(agent, data)
            build_turns(agent.messages[1:])  # 화면에 그릴 수 있는 형태인지 미리 확인한다
            created = float(data.get("created", created))
        except Exception as exc:  # 손상된 저장 파일 때문에 앱이 멈추지 않게 새 대화로 시작한다
            errorlog.log_exception(f"저장된 대화 복원 실패 ({session_id})", exc)
            S.notice = "저장된 대화를 불러오지 못해서 새 대화로 시작했어요."
            agent = Agent(
                llm=factory(api_key), model="", root=root, tracker=CostTracker(),
                instructions=load_instructions(), processes=_process_manager(),
            )
            data, session_id = None, sessions.new_session_id()
    elif session_id:
        S.notice = "저장된 대화 파일이 손상되어 열 수 없어요. 새 대화로 시작했어요."
        session_id = sessions.new_session_id()
    if not data:
        session_id = session_id or sessions.new_session_id()
    S.agent, S.session_id, S.session_created = agent, session_id, created
    S.agent_root, S.agent_key = str(agent.root), api_key
    S.pending, S.saved_sig = None, None
    # 승인을 기다리던 도구 호출이 있었다면 이어서 처리해 승인 카드를 다시 보여준다
    S.run_requested = agent.has_pending_calls()


def on_switch_session(session_id: str) -> None:
    autosave(force=True)
    open_session(S.agent.root, session_id)


def on_new_chat() -> None:
    autosave(force=True)
    open_session(S.agent.root, new=True)


def on_delete_session(session_id: str) -> None:
    root = S.agent.root
    sessions.delete(root, session_id)
    if session_id == S.session_id:
        open_session(root, new=True)


def on_delete_all_sessions() -> None:
    root = S.agent.root
    sessions.delete_all(root)
    open_session(root, new=True)


def on_stop_process(proc_id: str) -> None:
    if S.get("agent") is not None:
        try:
            S.agent.processes.stop(proc_id)
        except Exception as exc:
            errorlog.log_exception(f"프로그램 중지 실패 ({proc_id})", exc)
            S.notice = f"프로그램을 중지하지 못했어요: {exc}"


def on_check_key() -> None:
    S.key_info, S.key_error = None, None
    try:
        S.key_info = fetch_key_info(S.get("api_key", "").strip())
    except LLMError as exc:
        S.key_error = str(exc)


def on_decide(call_id: str, approved: bool) -> None:
    S.agent.decide(call_id, approved)
    S.pending = None
    S.run_requested = True
    autosave(force=True)


# ------------------------------------------------------------------ 그리기


def render_tool(entry: ToolEntry, preview: str | None) -> None:
    with st.expander(tool_title(entry), expanded=entry.status in ("error", "warn")):
        if entry.name in DIFF_TOOLS and preview:
            st.code(preview, language="diff")
        elif entry.name in ("run_command", "start_process"):
            st.code("$ " + str(entry.args.get("command", "")), language="bash")
        if entry.output:
            shown = entry.output if len(entry.output) <= 3000 else entry.output[:3000] + "\n... (이하 생략)"
            st.code(shown, language=None)


def render_attachment(view: AttachmentView) -> None:
    if view.image is not None:
        st.image(view.image, width=240, caption=f"📎 {view.path}")
    else:
        st.caption(f"📎 {view.path}" + (f" · {view.note}" if view.note else ""))


def render_history(agent: Agent) -> None:
    for turn in build_turns(agent.messages[1:]):
        with st.chat_message(turn.role):
            for block in turn.blocks:
                if block.attachment is not None:
                    render_attachment(block.attachment)
                elif block.tool is None:
                    st.markdown(block.text)
                elif block.tool.output is not None:  # 결과 없는 호출은 승인 카드/실행 중 표시가 담당
                    render_tool(block.tool, agent.previews.get(block.tool.id))


def render_cost(placeholder, agent: Agent | None, limit: float) -> None:
    tracker = agent.tracker if agent else CostTracker(limit_usd=limit)
    with placeholder.container():
        st.metric("이번 세션 비용", f"${tracker.cost_usd:.4f}")
        if limit > 0:
            st.progress(min(tracker.cost_usd / limit, 1.0), text=f"한도 ${limit:.2f}")
        st.caption(f"토큰 {tracker.total_tokens:,}")
        if tracker.unpriced_calls:
            st.caption("⚠️ 일부 호출은 비용이 집계되지 않았습니다. 한도가 정확하지 않을 수 있어요.")


def render_approval(request: ApprovalRequest) -> None:
    entry = ToolEntry(request.call.id, request.call.name, request.args)
    with st.chat_message("assistant"):
        st.warning(f"**승인이 필요해요** · {tool_title(entry).removeprefix('⏳ ')}")
        if entry.name in ("run_command", "start_process"):
            st.caption("이 명령은 내 컴퓨터에서 실제로 실행됩니다. 무슨 일을 하는지 이해되지 않으면 거절하세요.")
        st.code(request.preview, language="diff" if entry.name in DIFF_TOOLS else "bash")
        approve, reject, _ = st.columns([1, 1, 4])
        approve.button("✅ 승인", type="primary", key=f"ok-{entry.id}", on_click=on_decide, args=(entry.id, True))
        reject.button("🚫 거절", key=f"no-{entry.id}", on_click=on_decide, args=(entry.id, False))


def render_processes(placeholder, agent: Agent | None) -> None:
    """실행 중인 백그라운드 프로세스(접속 링크, 중지 버튼)."""
    running = [p for p in agent.processes.running() if p.cwd == str(agent.root)] if agent else []
    with placeholder.container():
        if not running:
            return
        st.divider()
        st.caption("▶️ 실행 중인 프로그램")
        for proc in running:
            st.markdown(f"**{proc.name}**  \n`{proc.command[:60]}`")
            for url in proc.urls()[:2]:
                st.link_button(f"🔗 {url}", url)
            st.button("⏹️ 중지", key=f"stop-{proc.id}", on_click=on_stop_process, args=(proc.id,))


def _ago(timestamp: float) -> str:
    seconds = max(time.time() - timestamp, 0)
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 86400:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def render_sessions(placeholder, agent: Agent | None) -> None:
    """이 폴더의 저장된 대화 목록 (전환, 삭제, 새 대화)."""
    if agent is None:
        placeholder.empty()
        return
    infos = sessions.list_sessions(agent.root)
    with placeholder.container():
        st.divider()
        st.caption("💬 이 폴더의 대화")
        st.button("➕ 새 대화", key="new-chat", on_click=on_new_chat, use_container_width=True)
        if S.get("save_failed"):
            st.warning("⚠️ 대화를 저장하지 못했어요. 디스크 공간이나 폴더 권한을 확인해 주세요. 지금 대화는 이 창에서만 유지돼요.")
        if not any(i.id == S.session_id for i in infos):
            st.caption("● 지금 대화 (아직 저장 전)")
        for info in infos[:15]:
            current = info.id == S.session_id
            left, right = st.columns([5, 1])
            left.button(
                ("● " if current else "") + info.title, key=f"sess-{info.id}", on_click=on_switch_session,
                args=(info.id,), disabled=current, use_container_width=True,
                help=f"{_ago(info.updated)} · 메시지 {info.messages}개 · ${info.cost:.3f}",
            )
            right.button("🗑", key=f"del-{info.id}", on_click=on_delete_session, args=(info.id,), help="이 대화 삭제")
        if len(infos) > 15:
            st.caption(f"오래된 대화 {len(infos) - 15}개는 목록에 표시하지 않아요")
        with st.expander("대화 저장 설정"):
            st.toggle("💾 대화 기록 저장", value=True, key="save_sessions", help="끄면 이 컴퓨터에 대화를 남기지 않아요 (공용 PC용)")
            st.caption("저장 위치: 내 계정의 `.vibe-agent/sessions` 폴더. API 키는 저장하지 않아요. 대화에는 코드와 파일 내용이 들어 있어요.")
            if infos:
                st.button("이 폴더의 대화 모두 삭제", key="del-all", on_click=on_delete_all_sessions)


def render_welcome() -> None:
    st.title("🤖 Vibe Agent")
    st.markdown(
        """
채팅으로 코드를 만들고 고치는 수업용 코딩 에이전트입니다.

**시작하기**
1. 왼쪽에 **OpenRouter API 키**를 붙여넣으세요. (이 컴퓨터의 메모리에만 있고 파일로 저장되지 않아요)
2. **작업 폴더**를 정하세요. 에이전트는 이 폴더 안에서만 읽고 쓸 수 있어요. 새 폴더를 만들어 시작하는 걸 추천해요.
3. 아래 채팅창에 만들고 싶은 것을 말해 보세요. 예: *"간단한 할 일 목록 웹페이지를 만들어줘"*

**알아두세요**
- 파일을 고치거나 명령을 실행하기 전에 항상 **승인**을 물어봅니다.
- 사용한 금액은 왼쪽에 실시간으로 표시되고, 정한 한도에 닿으면 멈춥니다.
"""
    )


# ------------------------------------------------------------------ 실행


def drive(agent: Agent, cost_placeholder, limit: float) -> None:
    """에이전트를 실행하면서 결과를 실시간으로 그린다. 승인이 필요하면 S.pending에 담고 멈춘다."""
    text_ph, buffer, tool_phs = None, "", {}
    try:
        with st.chat_message("assistant"):
            for event in agent.run():
                if isinstance(event, TextDelta):
                    if text_ph is None:
                        text_ph, buffer = st.empty(), ""
                    buffer += event.text
                    text_ph.markdown(buffer + "▌")
                    continue
                if text_ph is not None:
                    text_ph.markdown(buffer)
                    text_ph = None
                if isinstance(event, ToolStart):
                    entry = ToolEntry(event.call.id, event.call.name, event.args)
                    tool_phs[event.call.id] = st.empty()
                    tool_phs[event.call.id].caption(tool_title(entry) + " 실행 중…")
                elif isinstance(event, ToolResult):
                    autosave()
                    entry = ToolEntry(event.call.id, event.call.name, event.args, event.output)
                    preview = agent.previews.get(event.call.id)
                    target = tool_phs[event.call.id].container() if event.call.id in tool_phs else st.container()
                    with target:
                        render_tool(entry, preview)
                elif isinstance(event, ApprovalRequest):
                    S.pending = event
                elif isinstance(event, UsageUpdate):
                    render_cost(cost_placeholder, agent, limit)
                elif isinstance(event, Notice):
                    st.caption("ℹ️ " + event.message)
                elif isinstance(event, ErrorEvent):
                    st.error(event.message)
                elif isinstance(event, Done) and event.message:
                    st.warning(event.message)
            if text_ph is not None:
                text_ph.markdown(buffer)
        S.run_requested = False
    except Exception as exc:  # 화면이 깨지지 않게 안내하고, 대화 기록은 유지한다
        S.run_requested = False
        details = errorlog.log_exception("에이전트 실행 중 오류", exc)
        st.error(f"앱 내부 오류가 발생했어요: {type(exc).__name__}: {exc}")
        with st.expander("오류 자세히 보기 (강사에게 보여 주세요)"):
            st.code(details, language=None)
            st.caption(f"오류 기록 파일: {errorlog.LOG_PATH}")
    autosave(force=True)


# ------------------------------------------------------------------ 메인

_process_manager()
_init_state()
choices = model_catalog.load_model_choices()
labels = {m["id"]: m.get("label", m["id"]) for m in choices}

with st.sidebar:
    st.title("🤖 Vibe Agent")
    cost_placeholder = st.empty()  # 항상 보이도록 맨 위에 둔다
    process_placeholder = st.empty()

    st.divider()
    st.text_input("OpenRouter API 키", type="password", key="api_key", placeholder="sk-or-...")
    st.button("키 확인", on_click=on_check_key, help="키가 유효한지, 이 키에 설정된 한도가 얼마나 남았는지 확인해요 (OpenRouter 계정 잔액이 아니에요)")
    if S.key_error:
        st.error(S.key_error)
    elif S.key_info:
        remaining = S.key_info.limit_remaining
        st.success(
            "키 확인 완료 · "
            + (
                f"이 키의 남은 한도 ${remaining:.2f} (확인한 시점 기준)"
                if remaining is not None
                else "이 키에는 한도가 설정되어 있지 않아요 (계정 크레딧이 떨어질 때까지 사용 가능)"
            )
        )

    st.divider()
    st.text_input("작업 폴더 경로", key="root_text", placeholder=r"예: C:\Users\me\my-project")
    st.button("📂 폴더 선택…", on_click=on_pick_folder)
    if S.notice:
        st.info(S.notice)
        S.notice = None
    session_placeholder = st.empty()  # 이 폴더의 대화 목록 (에이전트가 준비된 뒤에 채운다)

    st.divider()
    catalog = model_catalog.fetch_catalog()
    extra_labels = {BROWSE_MODEL: "🔎 전체 모델에서 찾기…", CUSTOM_MODEL: "직접 입력…"}
    st.selectbox(
        "모델",
        [m["id"] for m in choices] + [BROWSE_MODEL, CUSTOM_MODEL],
        key="model_choice",
        format_func=lambda i: extra_labels.get(i) or labels.get(i, i),
    )
    if S.model_choice == CUSTOM_MODEL:
        st.text_input("모델 ID", key="custom_model", placeholder="provider/model-name")
        model = S.custom_model.strip()
    elif S.model_choice == BROWSE_MODEL:
        browse = model_catalog.browseable_models(catalog)
        if browse:
            by_id = {m.id: m for m in browse}
            st.selectbox(
                "모델 검색 (글자를 입력해 찾을 수 있어요)",
                list(by_id),
                index=None,
                placeholder="모델을 선택하세요",
                key="browse_model",
                format_func=lambda i: model_catalog.describe(by_id[i]),
            )
            model = S.get("browse_model") or ""
        else:
            st.info("모델 목록을 불러올 수 없어요. 인터넷 연결을 확인하거나 '직접 입력…'을 쓰세요.")
            model = ""
    else:
        model = S.model_choice
    info = catalog.get(model)
    if catalog and model and info is None:
        st.warning("OpenRouter 목록에 없는 모델 ID예요. 철자를 확인하세요.")
    elif info and info.supports_tools is False:
        st.warning("이 모델은 도구 호출을 지원하지 않아 코딩 에이전트로 쓸 수 없어요.")
    elif info and info.prompt_per_m is not None and info.completion_per_m is not None:
        st.caption(f"가격(100만 토큰당): 입력 ${info.prompt_per_m:.2f} · 출력 ${info.completion_per_m:.2f}")
    if model_catalog.is_expensive(info):
        st.warning("비싼 모델이에요. 한도를 빨리 쓸 수 있으니 필요할 때만 쓰세요.")
    if info and info.supports_images is not None:
        st.caption("🖼️ 이미지 입력 가능" if info.supports_images else "🖼️ 이미지는 볼 수 없는 모델 (텍스트/코드 파일은 가능)")

    limit = st.number_input("세션 최대 비용 ($)", min_value=0.0, value=1.0, step=0.5, help="이 앱에서 이 대화가 쓸 수 있는 최대 금액이에요 (OpenRouter 키의 한도와는 별개). 닿으면 에이전트가 멈춰요. 0이면 제한 없음")
    auto_edits = st.toggle("파일 수정은 묻지 않고 진행", value=False, help="명령 실행은 계속 물어봐요. 변경 내용은 기록에서 볼 수 있어요.")
    max_steps = st.number_input("요청당 최대 단계", min_value=3, max_value=60, value=20, step=1)

    st.divider()
    st.caption("키를 바꿔도 대화는 이어져요. 폴더를 바꾸면 그 폴더의 대화로 전환돼요.")

api_key = S.get("api_key", "").strip()
root_text = S.root_text.strip().strip('"')
root = Path(root_text).expanduser() if root_text else None
if root_text and not (root and root.is_dir()):
    st.sidebar.error("폴더를 찾을 수 없어요. 경로를 확인해 주세요.")
ready = bool(api_key and model and root and root.is_dir())

agent: Agent | None = None
if ready:
    root_key = str(root.resolve())
    if S.agent is None or S.agent_root != root_key:
        autosave(force=True)  # 폴더를 바꾸면 이전 대화를 먼저 저장한다
        open_session(root)
        save_settings({"last_root": root_key, "last_model": model})
        if S.notice:
            st.sidebar.info(S.notice)
            S.notice = None
    elif api_key != S.agent_key:  # 키만 바뀌었으면 대화는 그대로 두고 연결만 바꾼다
        S.agent.llm = S.get("llm_factory", OpenRouterClient)(api_key)
        S.agent_key = api_key
    agent = S.agent
    agent.model = model
    agent.max_steps = int(max_steps)
    agent.tracker.limit_usd = limit or None
    agent.auto_approve = set(EDIT_AUTO_APPROVE) if auto_edits else set()
render_cost(cost_placeholder, agent, limit)

if agent is None:
    render_processes(process_placeholder, None)
    render_sessions(session_placeholder, None)
    render_welcome()
    st.chat_input("먼저 왼쪽에서 키와 작업 폴더를 설정해 주세요", disabled=True)
    st.stop()

rule_files = [p.name for p in find_rule_files(agent.root)]
st.caption(
    f"📂 {agent.root}  ·  🧠 {agent.model}" + (f"  ·  📘 프로젝트 지침: {', '.join(rule_files)}" if rule_files else "")
)
render_history(agent)

waiting = S.pending is not None
prompt = st.chat_input(
    "위의 승인 요청에 먼저 답해 주세요" if waiting else "무엇을 만들어볼까요? (📎 이미지·파일 첨부 가능)",
    disabled=waiting,
    accept_file="multiple",
    file_type=ALLOWED_EXTS,
)
if prompt and S.pending is None:
    uploads = [Upload(f.name, f.getvalue()) for f in prompt.files]
    prepared = prepare_message(prompt.text or "", uploads, agent.root, vision=info.supports_images if info else None)
    for note in prepared.notes:
        st.warning(note)
    if prepared.saved:
        st.toast(f"작업 폴더의 attachments/ 에 {len(prepared.saved)}개 저장했어요")
    if prepared.content:
        agent.submit(prepared.content)
        S.run_requested = True
        for turn in build_turns(agent.messages[-1:]):
            with st.chat_message("user"):
                for block in turn.blocks:
                    if block.attachment is not None:
                        render_attachment(block.attachment)
                    else:
                        st.markdown(block.text)

if S.run_requested:
    drive(agent, cost_placeholder, limit)
    if S.pending is not None:
        st.rerun()  # 입력창을 잠그고 승인 카드를 기록 아래에 정상 위치로 다시 그린다
if S.pending is not None:
    render_approval(S.pending)
autosave(force=True)
render_processes(process_placeholder, agent)
render_sessions(session_placeholder, agent)
