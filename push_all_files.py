# -*- coding: utf-8 -*-
"""
KOSPI Monitor — 모든 파일을 GitHub에 push (1회용 패치)

기존 push.bat은 docs/ 폴더만 push하므로 Python 스크립트가 GitHub에 없는 상태.
이 스크립트는 .gitignore가 허용하는 모든 파일을 한 번에 push 합니다.
"""
import os
import sys
import subprocess
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).parent.resolve()


def run_git(args, capture=True):
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(SCRIPT_DIR),
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode, (result.stdout or ""), (result.stderr or "")
    except FileNotFoundError:
        return 127, "", "git 명령을 찾을 수 없음"


def main():
    print("=" * 60)
    print("  모든 파일을 GitHub에 push (1회용 패치)")
    print("=" * 60)
    print()

    if not (SCRIPT_DIR / ".git").exists():
        print("[ERROR] git 저장소가 아닙니다.")
        print("[ERROR] github_setup.bat을 먼저 실행하세요.")
        input("\nEnter 키를 눌러 종료...")
        return 1

    # 1. 모든 변경 사항 stage (.gitignore가 민감 정보는 자동 제외)
    print("[*] 모든 파일 stage...")
    rc, _, err = run_git(["add", "-A"])
    if rc != 0:
        print(f"[ERROR] git add 실패: {err.strip()}")
        input("\nEnter 키를 눌러 종료...")
        return 1
    print()

    # 2. 변경 사항 확인
    print("[*] stage된 파일 목록:")
    rc, out, _ = run_git(["status", "--short"])
    if out.strip():
        for line in out.strip().split("\n"):
            print(f"    {line}")
    else:
        print("    (변경 사항 없음)")
    print()

    # 3. 변경 없으면 종료
    rc, _, _ = run_git(["diff", "--cached", "--quiet"])
    if rc == 0:
        print("[INFO] commit할 변경 사항이 없습니다.")
        print("       (이미 모든 파일이 GitHub에 있는 상태)")
        input("\nEnter 키를 눌러 종료...")
        return 0

    # 4. commit
    print("[*] 커밋...")
    rc, _, err = run_git([
        "commit", "-m",
        "Add Python scripts and workflows for GitHub Actions automation"
    ])
    if rc != 0:
        print(f"[ERROR] commit 실패: {err.strip()}")
        input("\nEnter 키를 눌러 종료...")
        return 1
    print("[+] 커밋 완료")
    print()

    # 5. push
    print("[*] GitHub에 push 중...")
    result = subprocess.run(
        ["git", "push"],
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode != 0:
        print()
        print("[ERROR] push 실패. 인증 또는 네트워크 문제일 수 있습니다.")
        input("\nEnter 키를 눌러 종료...")
        return 1

    print()
    print("=" * 60)
    print("  모든 파일 push 완료!")
    print()
    print("  이제 GitHub Actions가 정상 작동합니다.")
    print()
    print("  ▶ 즉시 테스트:")
    print("    https://github.com/sparem0918/kospi-monitor/actions")
    print("    Daily KOSPI Monitor → Run workflow → Run")
    print()
    print("  ▶ 결과 확인 (1~3분 후):")
    print("    https://sparem0918.github.io/kospi-monitor/")
    print("    \"생성: 시각\"이 방금으로 갱신되는지 확인")
    print("=" * 60)
    input("\nEnter 키를 눌러 종료...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[중단]")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[!] 오류: {e}\n")
        traceback.print_exc()
        input("\nEnter 키를 눌러 종료...")
        sys.exit(1)
