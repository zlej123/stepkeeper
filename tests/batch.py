#!/usr/bin/env python3
"""Run stratified structural and semantic regression checks.

Usage:
    py -3.11 tests/batch.py                  # cached analyses only
    py -3.11 tests/batch.py --analyze        # analyze missing fixtures
    py -3.11 tests/batch.py --force          # reanalyze every fixture
    py -3.11 tests/batch.py --capture        # generate candidate pickers

Semantic reviews live at tests/evaluations/<video>.<profile>.<language>.json.
The picker generates this JSON; copy it there to include top-3 hit rate.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
from stepkeeper.common import analysis_file  # noqa: E402
from stepkeeper.contract import validate  # noqa: E402


def video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
    return match.group(1) if match else url


def hms(seconds) -> str:
    if not isinstance(seconds, int):
        return "?"
    return f"{seconds // 60}:{seconds % 60:02d}"


def run_analyze(url: str, profile: str, language: str,
                model: str, force: bool) -> int:
    command = [sys.executable, "-m", "stepkeeper.analyze", url,
               "--profile", profile, "--language", language,
               "--model", model]
    if force:
        command.append("--force")
    result = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print((result.stdout or "")[-1000:])
        print((result.stderr or "")[-1000:])
    return result.returncode


def evaluation_file(vid: str, profile: str, language: str) -> Path:
    return HERE / "evaluations" / f"{vid}.{profile}.{language}.json"


def semantic_result(path: Path):
    if not path.exists():
        return 0, 0, None
    data = json.loads(path.read_text(encoding="utf-8"))
    reviewed = [guide for guide in data.get("guides", []) if guide.get("reviewed")]
    hits = sum(1 for guide in reviewed if guide.get("candidate_hit"))
    rate = hits / len(reviewed) if reviewed else None
    return len(reviewed), hits, rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    args = ap.parse_args()

    fixtures = json.loads((HERE / "fixtures" / "urls.json").read_text(encoding="utf-8"))
    dimensions = fixtures.get("_dimensions", {})
    coverage = defaultdict(Counter)

    lines = ["# stepkeeper 배치 검증 리포트", ""]
    total = passed = failed = skipped = 0
    semantic_reviewed = semantic_hits = 0
    quota_blocked = False

    for domain, config in fixtures.items():
        if domain.startswith("_") or not isinstance(config, dict):
            continue
        if args.domain and domain != args.domain:
            continue
        default_profile = config.get("profile", "generic")
        default_language = config.get("language", "ko")
        suite = config.get("suite", "domain")
        videos = config.get("videos", [])
        lines.append(
            f"## {domain} (default profile={default_profile}, "
            f"language={default_language}, suite={suite}, {len(videos)}개)\n")
        if not videos:
            lines.append("_영상 없음 — fixtures/urls.json에 추가 필요._\n")
            continue
        lines.extend([
            "| video | lang | 제목 | 단계 | 가이드 | 구조 | 의미 검토 | 상세 |",
            "|-------|------|------|------|--------|------|-----------|------|",
        ])
        domain_reviewed = 0
        domain_hits = 0

        for fixture in videos:
            total += 1
            profile = fixture.get("profile", default_profile)
            language = fixture.get("language", default_language)
            vid = video_id(fixture["url"])
            for name, value in fixture.get("strata", {}).items():
                coverage[name][value] += 1
            source = analysis_file(ROOT, vid, profile, language)
            if quota_blocked and not source.exists():
                skipped += 1
                lines.append(
                    f"| {vid} | {language} | {fixture.get('note','')} | - | - | 스킵 | - | "
                    "Gemini 한도 도달 후 실행 보류 |")
                continue
            if args.force or (args.analyze and not source.exists()):
                returncode = run_analyze(
                    fixture["url"], profile, language, args.model, args.force)
                if returncode == 75:
                    quota_blocked = True
                    skipped += 1
                    lines.append(
                        f"| {vid} | {language} | {fixture.get('note','')} | - | - | 스킵 | - | "
                        "Gemini 무료 티어 한도 도달 |")
                    continue
                if returncode != 0:
                    failed += 1
                    lines.append(
                        f"| {vid} | {language} | {fixture.get('note','')} | - | - | 실패 | - | "
                        "analyze 실패 |")
                    continue
            if not source.exists():
                skipped += 1
                lines.append(
                    f"| {vid} | {language} | {fixture.get('note','')} | - | - | 스킵 | - | "
                    "--analyze 필요 |")
                continue

            data = json.loads(source.read_text(encoding="utf-8"))
            errors, warnings = validate(data)
            if data.get("_output_language") and data["_output_language"] != language:
                errors = list(errors) + [
                    f"_output_language={data['_output_language']!r} != fixture {language!r}"]
            guides = data.get("visual_guides", [])
            reviewed, hits, rate = semantic_result(
                evaluation_file(vid, profile, language))
            semantic_reviewed += reviewed
            semantic_hits += hits
            domain_reviewed += reviewed
            domain_hits += hits
            semantic_text = "미검토" if rate is None else f"{hits}/{reviewed} ({rate:.0%})"
            below_target = rate is not None and rate < 0.90
            ok = not errors
            if ok:
                passed += 1
            else:
                failed += 1
            detail_parts = []
            if errors:
                detail_parts.append("ERR: " + "; ".join(errors))
            if below_target:
                detail_parts.append("상위3 후보 적중률 <90%")
            if warnings and not detail_parts:
                detail_parts.append(f"WARN×{len(warnings)}")
            lines.append(
                f"| {vid} | {language} | {(data.get('title') or '?')[:30]} | "
                f"{len(data.get('steps', []))} "
                f"| {len(guides)} | {'통과' if not errors else '실패'} | {semantic_text} "
                f"| {'; '.join(detail_parts)} |")

            if guides:
                lines.append("")
                for guide in guides:
                    timestamp = guide.get("best_visual_timestamp")
                    link = (f"https://youtu.be/{vid}?t={timestamp}"
                            if isinstance(timestamp, int) else "(링크없음)")
                    lines.append(
                        f"- `{guide['id']}` `{guide.get('type')}` "
                        f"**{guide.get('phrase')}** → {hms(timestamp)} {link}")
                lines.append("")
            for warning in warnings:
                lines.append(f"  - 경고: {warning}")
            if warnings:
                lines.append("")

            if args.capture and not errors and guides:
                subprocess.run([
                    sys.executable, "-m", "stepkeeper.capture", vid,
                    "--profile", profile, "--language", language,
                ], cwd=str(ROOT))
        if domain_reviewed:
            lines.append(
                f"**{domain} 상위 3개 후보 적중률: "
                f"{domain_hits}/{domain_reviewed} "
                f"({domain_hits/domain_reviewed:.0%})**")
        lines.append("")

    lines.append("## 층화 커버리지\n")
    lines.append("| 차원 | 관측 분포 | 미포함 값 |")
    lines.append("|------|-----------|-----------|")
    for dimension, expected in dimensions.items():
        observed = coverage[dimension]
        missing = [value for value in expected if not observed[value]]
        distribution = ", ".join(f"{key}:{value}" for key, value in sorted(observed.items())) or "없음"
        lines.append(f"| {dimension} | {distribution} | {', '.join(missing) or '없음'} |")

    semantic_rate = (semantic_hits / semantic_reviewed
                     if semantic_reviewed else None)
    semantic_suite_failed = semantic_rate is not None and semantic_rate < 0.90
    summary = f"**합계 {total} / 통과 {passed} / 실패 {failed} / 스킵 {skipped}**"
    semantic_summary = ("미검토" if semantic_rate is None else
                        f"{semantic_hits}/{semantic_reviewed} ({semantic_rate:.0%})")
    lines.insert(1, summary + f"  \n**상위 3개 후보 의미 적중률: {semantic_summary}**\n")

    report = HERE / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary)
    print(f"상위 3개 후보 의미 적중률: {semantic_summary}")
    print(f"리포트: {report}")
    if failed or semantic_suite_failed:
        sys.exit(1)
    # fail-closed (외부 리뷰 #7): "전체 스킵 + 분석 0개"가 exit 0이면 CI 초록불이
    # 실제 품질을 전혀 의미하지 않는다. 검증한 것이 하나도 없으면 성공이 아니다.
    if total and passed == 0:
        sys.exit(f"batch: {total}개 중 통과 0개 (스킵 {skipped}) — "
                 "아무것도 검증되지 않았으므로 실패로 처리합니다. "
                 "(--analyze로 분석을 생성했는지, 캐시 경로가 맞는지 확인)")


if __name__ == "__main__":
    main()
