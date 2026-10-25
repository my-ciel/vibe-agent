# Vibe Agent

OpenRouter API 키만으로 쓰는 수업용 채팅형 코딩 에이전트 (Streamlit).

## 실행

- 윈도우: `run.bat` 더블클릭 / 맥: `run.command` 더블클릭 (uv가 없으면 설치 여부를 물어봄)
- 터미널: `uv run python launch.py` (가상환경 활성화 불필요)
- 환경 점검: `run.bat --check` (맥: `bash run.command --check`)

수강생 안내는 `docs/수강생_시작가이드.md`, 강사용 안내는 `docs/강사용_가이드.md`, 윈도우 확인 항목은 `docs/윈도우_테스트_체크리스트.md`를 보세요.

1. 사이드바에 OpenRouter API 키 입력 (메모리에만 보관, 파일로 저장하지 않음. 대화는 `~/.vibe-agent/sessions`에 폴더별로 자동 저장되어 새로고침/재시작 후에도 이어지고, 사이드바에서 여러 대화를 전환할 수 있음)
2. 작업 폴더 지정 (에이전트는 이 폴더 밖을 읽거나 쓸 수 없음)
3. 채팅으로 요청. 파일 수정/명령 실행은 승인 후 실행됨

터미널 데모: `OPENROUTER_API_KEY=... python -m vibe_agent.cli --root 폴더`

## 강사용

- `tools/issue_keys.py`: 수강생별 한도/만료 키 일괄 발급
- `models.json`: 수강생에게 보여줄 모델 목록. ID가 정확한지 사이드바에서 자동 검증됨(인터넷 연결 시)
- 비용 통제: OpenRouter 대시보드에서 수강생별 키를 만들고 크레딧 한도/만료일을 설정. 앱의 "세션 최대 비용"은 추가 안전장치
- 명령 차단 목록은 실수 방지용이며 실제 방어선은 사용자 승인

## 개발

```
uv pip install -e ".[dev]" && pytest
DEMO_ROOT=폴더 streamlit run tests/demo_app.py   # 가짜 LLM으로 화면 확인
```
