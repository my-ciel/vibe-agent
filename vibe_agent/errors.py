class ToolError(Exception):
    """도구 실행 실패. 메시지는 모델과 사용자에게 그대로 전달된다."""


class SandboxError(ToolError):
    """작업 폴더 밖 접근 등 샌드박스 정책 위반."""
