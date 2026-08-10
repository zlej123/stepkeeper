#!/usr/bin/env python3
"""End-to-end orchestrator: URL -> analysis -> (frames) -> document -> export.

This is the single entry point that any shell (AI-tool skill, Apple Shortcut,
REST API, desktop app) can call.

Two paths:
  1. --links-only: analyze then render with timestamp-link fallback only.
     No ffmpeg, no capture, one shot, fully automatic.
  2. default: analyze then capture candidates. Rendering waits for an explicit
     picks.json (from picker.html). Without picks it renders link-only and
     prints the picker path so a human/agent can choose, then rerun.

Usage:
    py -3.11 pipeline.py URL [--profile generic] [--language ko] [--max-guides 5]
        [--model gemini-3.5-flash-lite] [--force]
        [--links-only] [--picks PATH]
        [--export bundle|obsidian|goodnotes|notion] [--destination DIR]
        [--parent PAGE_ID]   # required for --export notion
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from .common import analysis_file, data_root, frames_dir, video_id

sys.stdout.reconfigure(encoding="utf-8")


def command(module: str, *args: str) -> list:
    """하위 모듈 명령 조립 — 위치 인자(video_id/url)를 "--" 뒤에 둔다."""
    positional, *rest = args
    return [sys.executable, "-m", f"stepkeeper.{module}", *rest, "--", positional]


def run(module: str, *args: str) -> None:
    """하위 모듈 실행. 첫 인자(video_id)는 "--" 뒤에 둔다 —
    유튜브 ID는 '-'로 시작할 수 있고(base64url), 그러면 argparse가 옵션으로 읽어
    "the following arguments are required: video_id"로 죽는다 (실측: -kIaNu00a4s).
    """
    result = subprocess.run(command(module, *args))
    if result.returncode != 0:
        sys.exit(f"[pipeline] {module} 실패 (exit {result.returncode})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--profile", default="generic")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--max-guides", default="5")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--links-only", action="store_true",
                    help="캡처 없이 타임스탬프 링크만으로 렌더 (완전 자동)")
    ap.add_argument("--picks", help="picker.html에서 내려받은 picks.json")
    ap.add_argument("--auto-pick", action="store_true",
                    help="캡처 후 AI가 후보 3장 중 장면을 자동 선택 (사람 검토는 picker에서)")
    ap.add_argument("--export", choices=("bundle", "obsidian", "goodnotes", "notion"))
    ap.add_argument("--export-draft", action="store_true",
                    help="검수 전 상태(자동 선택·선택 없음)에서도 내보내기 허용")
    ap.add_argument("--destination")
    ap.add_argument("--parent", help="Notion 부모 페이지 ID (--export notion)")
    ap.add_argument("--notion-token",
                    help="Notion integration token (기본: NOTION_TOKEN 환경변수)")
    args = ap.parse_args()

    if args.export == "notion" and not args.parent:
        ap.error("--export notion에는 --parent <페이지 ID>가 필요합니다.")

    try:
        vid = video_id(args.url)
    except ValueError as error:
        sys.exit(str(error))
    common_flags = ["--profile", args.profile, "--language", args.language]

    print("[pipeline] 1) 분석")
    analyze_flags = ["--model", args.model, "--max-guides", str(args.max_guides)]
    if args.force:
        analyze_flags.append("--force")
    run("analyze", args.url, *common_flags, *analyze_flags)

    render_flags = list(common_flags)
    if args.links_only:
        print("[pipeline] 2) 렌더 (링크 전용)")
    else:
        print("[pipeline] 2) 후보 프레임 추출")
        run("capture", vid, *common_flags)
        if args.auto_pick:
            print("[pipeline] 2.5) AI 장면 선택")
            run("autopick", vid, *common_flags, "--model", args.model)
        picks = args.picks
        if not picks:
            default_picks = frames_dir(data_root(), vid, args.profile, args.language) / "picks.json"
            if default_picks.exists():
                picks = str(default_picks)
        if picks:
            render_flags += ["--picks", picks]
        else:
            picker = frames_dir(data_root(), vid, args.profile, args.language) / "picker.html"
            print(f"[pipeline] 선택 파일 없음 -> 링크 전용으로 렌더합니다.")
            print(f"[pipeline] 사진을 넣으려면 {picker} 에서 선택 후 "
                  f"--picks <picks.json>로 다시 실행하세요.")
        print("[pipeline] 3) 렌더")
    run("render", vid, *render_flags)

    if args.export:
        # 검수 전 초안을 완성본처럼 내보내지 않는다 (외부 리뷰: "완료되지 않은 결과도
        # 완료처럼 보인다"). 사람이 고른 --picks나 의도된 --links-only는 통과, 자동 선택
        # 또는 선택 없음은 --export-draft를 명시해야 내보낸다.
        draft_reason = None
        if not args.links_only and not args.picks:
            meta_path = frames_dir(data_root(), vid, args.profile, args.language) / "picks-meta.json"
            if picks and meta_path.exists() and                     json.loads(meta_path.read_text(encoding="utf-8")).get("source") == "auto":
                draft_reason = "AI 자동 선택이 아직 검수되지 않았습니다"
            elif not picks:
                draft_reason = "프레임 선택이 없어 링크 전용 초안입니다"
        if draft_reason and not args.export_draft:
            picker = frames_dir(data_root(), vid, args.profile, args.language) / "picker.html"
            sys.exit(f"[pipeline] 내보내기 중단: {draft_reason}.\n"
                     f"  검수: {picker} 에서 확인 후 --picks <picks.json>으로 재실행\n"
                     f"  초안임을 알고 내보내려면 --export-draft를 추가하세요.")
        print(f"[pipeline] 4) 내보내기 ({args.export})")
        export_flags = [vid, *common_flags, "--target", args.export]
        if args.destination:
            export_flags += ["--destination", args.destination]
        if args.export == "notion":
            export_flags += ["--parent", args.parent]
            if args.notion_token:
                export_flags += ["--notion-token", args.notion_token]
        run("export", *export_flags)

    print(f"[pipeline] 완료: {analysis_file(data_root(), vid, args.profile, args.language)}")


if __name__ == "__main__":
    main()
