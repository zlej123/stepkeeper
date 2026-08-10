#!/usr/bin/env python3
"""AI frame selection: Gemini vision picks one candidate per visual guide.

Usage:
    python -m stepkeeper.autopick VIDEO_ID --profile generic --language ko

Reads the analysis and the before/center/after candidates from disk, asks
Gemini which frame actually shows each guide's `what_to_show` (or none),
writes picks.json (+ picks-meta.json with reasons), and regenerates
picker.html with the AI picks pre-selected so a human can review and export
a feedback record.
"""
import argparse
import base64
import json
import os
import sys

from .analyze import RateLimitError, generate_json
from .capture import SLOTS, build_picker
from .common import analysis_file, data_root, frames_dir

sys.stdout.reconfigure(encoding="utf-8")

PICK_SCHEMA = {
    "type": "object",
    "required": ["picks"],
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["guide_id", "slot", "reason"],
                "properties": {
                    "guide_id": {"type": "string"},
                    "slot": {"enum": ["before", "center", "after", "none"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

PROMPT = """당신은 시각 가이드용 대표 프레임을 고르는 검수자입니다.
각 가이드마다 후보 3장(before/center/after)이 순서대로 첨부됩니다.
가이드의 '보여야 할 것'이 실제로 가장 명확하게 보이는 후보 하나를 고르세요.
동작을 보여야 하는 가이드는 동작이 수행되는 중인 순간이 담긴 후보를 고르세요 — 완성된 결과물만 보이는 후보는 그 동작을 보여주지 못한 것입니다.
가이드가 특정 도구나 재료를 요구하면 그것이 실제로 보이는 후보만 유효합니다 — 어느 후보에도 보이지 않으면 "none"입니다.
세 장 모두에서 그것이 보이지 않으면 반드시 "none"을 고르세요 — 억지로 고르지 않습니다.
각 선택에 한 문장 근거(reason)를 답하세요. JSON만 출력합니다."""

VERIFY_SCHEMA = {
    "type": "object",
    "required": ["shows"],
    "properties": {
        "shows": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

VERIFY_PROMPT = """당신은 선택된 프레임을 검증하는 검수자입니다.
아래 '보여야 할 것'과 이 프레임을 비교하세요.
요구된 대상·도구·동작이 프레임에 **아예 없을 때만** shows=false입니다.
각도가 아쉽거나 일부가 가려졌거나 동작의 앞뒤 순간이더라도, 그 내용을 알아볼 수 있으면 shows=true입니다.
이 판정은 문서에 넣을 사진을 버리는 결정이므로, 확신이 없으면 shows=true를 고르세요. JSON만 출력합니다."""


def auto_pick(vid: str, profile: str, language: str, model: str, key: str) -> dict:
    """Run AI selection; returns picks dict and writes picks.json / picks-meta.json."""
    source = analysis_file(data_root(), vid, profile, language)
    if not source.exists():
        raise FileNotFoundError(f"분석 결과 없음: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    frames = frames_dir(data_root(), vid, profile, language)

    guides = [guide for guide in data.get("visual_guides", [])
              if guide.get("best_visual_timestamp") is not None]
    # 가이드별 **독립 호출** (외부 리뷰 P2-4): 여러 가이드를 한 요청에 넣었더니
    # 앞 가이드의 장면 설명이 뒤 가이드의 근거에 새어 들어왔다 (실측 ㄱ20 —
    # reason이 이전 가이드의 자세를 반복). 토큰은 이미지 수로 정해지므로 총량은
    # 같고 요청 횟수만 늘어난다.
    picks = {}
    reasons = {}
    asked = []
    for guide in guides:
        candidates = {slot: frames / f"{guide['id']}_{slot}.jpg" for slot in SLOTS}
        if not all(path.exists() for path in candidates.values()):
            continue
        asked.append(guide["id"])
        parts = [{"text": PROMPT},
                 {"text": (
                     f"[{guide['id']}] 표현: {guide.get('phrase', '')}\n"
                     f"보여야 할 것: {guide.get('what_to_show', '')}\n"
                     f"가이드: {guide.get('guide_text', '')}")}]
        for slot in SLOTS:
            parts.append({"text": f"{guide['id']} 후보 {slot}:"})
            parts.append({"inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(candidates[slot].read_bytes()).decode(),
            }})
        response = generate_json(parts, model, key, PICK_SCHEMA)
        for item in response.get("picks", []):
            if item.get("guide_id") == guide["id"] \
                    and item.get("slot") in (*SLOTS, "none"):
                picks[item["guide_id"]] = item["slot"]
                reasons[item["guide_id"]] = item.get("reason", "")
        # 자기 검증 패스: 고른 한 장만 다시 보여 "정말 보이는가"를 묻는다.
        # 후보 3장을 비교하며 생기는 "그중 제일 낫다" 편향을 끊는 안전망 —
        # 틀린 사진이 조용히 문서에 들어가는 것이 최악의 실패다 (실측 #6:
        # 렌치가 어느 후보에도 없는데 center를 골랐다).
        slot = picks.get(guide["id"])
        if slot and slot != "none":
            verify = generate_json([
                {"text": VERIFY_PROMPT},
                {"text": f"보여야 할 것: {guide.get('what_to_show', '')}"},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(candidates[slot].read_bytes()).decode(),
                }},
            ], model, key, VERIFY_SCHEMA)
            if not verify.get("shows"):
                picks[guide["id"]] = "none"
                reasons[guide["id"]] = verify.get("reason") or "검증 실패: 요구된 내용이 보이지 않음"
    if not asked:
        raise FileNotFoundError(f"후보 프레임 없음: {frames} (capture를 먼저 실행)")
    for guide_id in asked:  # 모델이 빠뜨린 가이드는 안전하게 링크 폴백
        picks.setdefault(guide_id, "none")

    (frames / "picks.json").write_text(
        json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    (frames / "picks-meta.json").write_text(json.dumps({
        "source": "auto", "model": model, "reasons": reasons,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--profile", default="generic")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY 환경변수가 없습니다.")
    try:
        picks = auto_pick(args.video_id, args.profile, args.language, args.model, key)
    except FileNotFoundError as error:
        sys.exit(str(error))
    except RateLimitError as error:
        print("Gemini 한도 도달:", str(error)[-300:])
        sys.exit(75)

    picked = sum(1 for slot in picks.values() if slot != "none")
    print(f"AI 선택 완료: 사진 {picked}개, 링크 폴백 {len(picks) - picked}개")
    for guide_id, slot in picks.items():
        print(f"  {guide_id}: {slot}")
    picker = build_picker(args.video_id, args.profile, args.language)
    print(f"검토용 picker (AI 선택 미리표시): {picker}")
    print("다르게 고쳤다면 semantic-evaluation.json을 내려받아 "
          "`python -m stepkeeper.feedback add <파일>` 로 기록하세요.")


if __name__ == "__main__":
    main()
