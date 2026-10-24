from vibe_agent import doctor
from vibe_agent.doctor import Check, format_report, run_checks


def test_checks_pass_in_this_environment():
    checks = run_checks(network=False)
    failed = [c for c in checks if c.status == "fail"]
    assert not failed, failed
    names = {c.name for c in checks}
    assert {"한글 경로/파일", "명령 실행(한글 출력)", "명령 시간 초과 처리", "작업 폴더 밖 접근 차단"} <= names


def test_crashing_check_is_reported_not_raised():
    def boom():
        raise RuntimeError("x")

    result = doctor._safe("이름", boom)
    assert result.status == "fail" and "RuntimeError" in result.detail


def test_report_summarizes_failures():
    text = format_report([Check("a", "ok", "좋음"), Check("b", "fail", "나쁨"), Check("c", "warn", "주의")])
    assert "✅ a" in text and "❌ b" in text and "⚠️" in text and "문제가 1개" in text
    assert "모든 항목이 정상" in format_report([Check("a", "ok")])
