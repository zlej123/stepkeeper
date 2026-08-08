#!/usr/bin/env python3
"""Analyze a YouTube how-to video into normalized steps and visual guides.

Usage:
    py -3.11 analyze.py URL [--profile generic] [--language ko] [--max-guides 5]

The caller supplies the user-profile language. Results are cached per
video/profile/language under work/analyses/.
"""
import argparse
import json
import os
import re
import time
from urllib.error import HTTPError
import subprocess
import sys
import urllib.request
from pathlib import Path
from .common import UnknownProfileError, analysis_file, data_root, hms, video_id as parse_video_id
from .contract import validate
sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 콘솔 대응

PKG = Path(__file__).parent
RULES = (PKG / "skill-core" / "engine" / "rules.md").read_text(encoding="utf-8")
TYPE_ALIASES = {
    "shape": "state",
    "pattern": "texture",
    "direction": "position",
    "setting": "position",
    "location": "position",
    "length": "size",
}


class RateLimitError(RuntimeError):
    pass


def load_schema(profile: str) -> dict:
    path = PKG / "skill-core" / "profiles" / profile / "schema.json"
    if not path.exists():
        raise UnknownProfileError(f"알 수 없는 프로파일 스키마: {profile} ({path} 없음)")
    schema = json.loads(path.read_text(encoding="utf-8"))
    for metadata_key in ("$schema", "$comment", "title"):
        schema.pop(metadata_key, None)
    return schema


def asset_digest(profile: str) -> str:
    """rules.md + prompt.md + schema.json의 sha256 앞 12자리 (외부 리뷰 #6).

    분석 JSON에 _asset_digest로 스탬프된다 — "이 결과가 어떤 프롬프트·스키마로
    만들어졌는가"를 추적할 수 있어, 품질 지표를 자산 버전별로 분리할 근거가 된다.
    """
    import hashlib
    digest = hashlib.sha256()
    digest.update((PKG / "skill-core" / "engine" / "rules.md").read_bytes())
    for name in ("prompt.md", "schema.json"):
        path = PKG / "skill-core" / "profiles" / profile / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def load_prompt(profile: str, duration_hms: str, language: str, max_guides: int) -> str:
    p = PKG / "skill-core" / "profiles" / profile / "prompt.md"
    if not p.exists():
        raise UnknownProfileError(f"알 수 없는 프로파일: {profile} ({p} 없음)")
    return (p.read_text(encoding="utf-8")
            .replace("{{RULES}}", RULES)
            .replace("{DURATION}", duration_hms)
            .replace("{OUTPUT_LANGUAGE}", language)
            .replace("{MAX_VISUAL_GUIDES}", str(max_guides)))

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def video_id(url: str) -> str:
    """CLI helper: parse YouTube id or exit with a message."""
    try:
        return parse_video_id(url)
    except ValueError as error:
        sys.exit(str(error))


def fetch_duration(url: str) -> int:
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--skip-download",
                        "--print", "duration", url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        sys.exit(f"영상 길이 조회 실패:\n{r.stderr[-1000:]}")
    return int(r.stdout.strip())


def mmss_to_sec(v):
    """'MM:SS' 또는 'H:MM:SS' -> 초. 이미 숫자면 그대로."""
    if v is None or isinstance(v, int):
        return v
    parts = [int(p) for p in str(v).split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def generate_json(parts: list, model: str, key: str,
                  schema: dict, retries: int = 2) -> dict:
    """Call Gemini generateContent with arbitrary parts, returning parsed JSON."""
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "temperature": 0.2,
        },
    }
    request = urllib.request.Request(
        API.format(model=model),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                payload = json.loads(response.read().decode())
            break
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code != 429:
                raise RuntimeError(
                    f"Gemini HTTP {error.code}: {detail[-2000:]}") from error
            if attempt >= retries:
                raise RateLimitError(detail[-2000:]) from error
            retry_after = error.headers.get("Retry-After")
            delay = (int(retry_after) if retry_after and retry_after.isdigit()
                     else 5 * (2 ** attempt))
            print(f"[429] {delay}초 후 재시도 ({attempt + 1}/{retries})")
            time.sleep(delay)
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(
            "응답 파싱 실패:\n" +
            json.dumps(payload, ensure_ascii=False, indent=2))
    return json.loads(text)


# 같은 프롬프트를 두 번 돌리면 **서로 다른 가이드**가 나온다 (모델 서빙 비결정성 실측:
# 기준선 재실행만으로 가이드 수가 ±0.95개 흔들린다). 프롬프트를 고쳐 더 뽑으려던 시도는
# 두 번 다 노이즈에 묻혔지만, 여러 번 돌려 **합집합**을 취하면 2.8개 → 5.2개로 늘었다.
# 노이즈와 싸우는 대신 이용하는 쪽이다.
MERGE_SECONDS = 2          # 이보다 가까운 같은 단계의 가이드는 같은 순간으로 본다


def _step_for(timestamp: int, steps: list) -> int | None:
    """타임스탬프가 속한 단계 id. 실행마다 단계 구조가 달라지므로 시간으로 다시 잇는다."""
    if not steps:
        return None
    for step in steps:
        start, end = step.get("t_start"), step.get("t_end")
        if start is not None and end is not None and start <= timestamp <= end:
            return step["id"]
    # 시간이 없는 단계는 "0초에 있는 것"처럼 보여 엉뚱하게 최근접이 된다 (리뷰 실측:
    # t_start=None 단계가 5초 가이드를 10~50초 단계로부터 빼앗았다). 후보에서 뺀다.
    timed = [s for s in steps
             if s.get("t_start") is not None or s.get("t_end") is not None]
    if not timed:
        return steps[0]["id"]
    return min(timed, key=lambda s: min(
        abs(timestamp - s["t_start"]) if s.get("t_start") is not None else 10**9,
        abs(timestamp - s["t_end"]) if s.get("t_end") is not None else 10**9))["id"]


# 같은 내용을 다르게 쓴 가이드를 걸러내는 문턱. 실측: 합집합에서 "windowpane test showing
# translucent dough"와 "translucent windowpane test"가 둘 다 남아 거의 같은 사진이 두 장
# 들어갔다. 짧은 쪽이 긴 쪽에 얼마나 담기는지(포함률)로 재야 이런 재진술이 잡힌다.
MERGE_CONTAINMENT = 0.6
MIN_TOKENS_FOR_CONTAINMENT = 3   # 이보다 짧으면 시간 근접만으로 판단
_STOPWORDS = {"the", "a", "an", "and", "or", "of", "on", "in", "to", "with", "for",
              "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "로"}


def _tokens(guide: dict) -> set:
    text = f"{guide.get('phrase', '')} {guide.get('what_to_show', '')}".lower()
    words = "".join(ch if ch.isalnum() else " " for ch in text).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _same_guide(a: dict, b: dict) -> bool:
    if a.get("step_id") != b.get("step_id"):
        return False
    if abs((a.get("best_visual_timestamp") or 0)
           - (b.get("best_visual_timestamp") or 0)) <= MERGE_SECONDS:
        return True
    first, second = _tokens(a), _tokens(b)
    # 토큰이 한두 개뿐이면 포함률이 무의미하다 — "dough" 하나가 "knead the dough until
    # smooth and elastic"을 삼켜 240초 떨어진 다른 순간을 지웠다 (리뷰 실측).
    if min(len(first), len(second)) < MIN_TOKENS_FOR_CONTAINMENT:
        return False
    return len(first & second) / min(len(first), len(second)) >= MERGE_CONTAINMENT


# 되묻기가 만드는 쓰레기를 코드로 거른다. 프롬프트로 금지하려던 시도는 실패했다 —
# 금지 목록을 붙이니 진짜 가이드도 같이 사라지고(좋은 것 47→30개) 원문에 타임스탬프를
# 넣는 새 결함까지 생겼다. 출력 검사는 결정적이고, 라벨 82건에 대고 재보니 네 규칙
# 모두 정밀도 100%였다 (쓰레기 15개 제거, 진짜 가이드 손실 0).
_JUNK_TIMESTAMP = re.compile(r"^\s*\d{1,2}:\d{2}\s*$")
_UNIT = r"(?:mm|cm|inch(?:es)?|g|grams?|kg|oz|lbs?|%|밀리미터|센티|그램|킬로)"
_JUNK_SPEC = re.compile(rf"[\d.]+\s*-?\s*{_UNIT}\b", re.IGNORECASE)
_JUNK_FREQUENCY = re.compile(
    r"(every\s+(other\s+)?day|매일|이틀에|번 연속|몇 번|번갈아|회 반복)", re.IGNORECASE)
_JUNK_EFFORT = re.compile(
    r"(천천히|빠르게|slowly|quickly|세게|강하게|too crazy|토크|힘을 주|힘 조절)",
    re.IGNORECASE)


def junk_reason(guide: dict) -> str | None:
    """정지 화면으로 보여줄 수 없거나 텍스트로 이미 정확한 가이드면 사유를, 아니면 None.

    규칙 10("한 장의 정지 화면으로 확인할 수 없는 조언은 만들지 않는다")을 사후에
    강제하는 장치다. 판정이 결정적이라 재실행해도 같은 결과가 나온다.
    """
    phrase = guide.get("phrase") or ""
    source = guide.get("source_phrase") or ""
    both = f"{phrase} {source}"
    if _JUNK_TIMESTAMP.match(source):
        return f"원문이 타임스탬프({source.strip()}) — 영상에서 들리는 말이 아니다"
    if _JUNK_SPEC.search(phrase) and len(phrase.split()) <= 4:
        return f"수치·규격({phrase}) — 텍스트가 이미 정확해 사진이 더 알려줄 게 없다"
    if _JUNK_FREQUENCY.search(both):
        return "빈도·횟수 — 한 장의 정지 화면으로 보여줄 수 없다"
    if _JUNK_EFFORT.search(both):
        return "속도·힘 — 한 장의 정지 화면으로 보여줄 수 없다"
    return None


def drop_junk(guides: list) -> tuple[list, list]:
    """(남길 가이드, 버린 사유 목록)."""
    kept, dropped = [], []
    for guide in guides:
        reason = junk_reason(guide)
        if reason:
            dropped.append(f"{guide.get('phrase', '')}: {reason}")
        else:
            kept.append(guide)
    return kept, dropped


def renumber(guides: list) -> list:
    """id를 vg-1..vg-N으로 다시 매긴다. 자르고 나면 구멍이 생겨(vg-1, vg-4, vg-5)
    나중에 추가되는 가이드가 기존 id와 충돌한다 (리뷰 실측: 중복 id 계약 위반)."""
    for index, guide in enumerate(guides, start=1):
        guide["id"] = f"vg-{index}"
    return guides


def order_for_reading(guides: list) -> list:
    """문서에 실릴 순서 — 단계별로, 단계 안에서는 시간순.

    importance 순으로 두면 한 단계 안에서 뒤 순간이 앞 순간보다 먼저 인쇄된다 (리뷰 실측).
    """
    return sorted(guides, key=lambda g: (g.get("step_id") or 0,
                                         g.get("best_visual_timestamp") or 0))


def trim_guides(guides: list, max_guides: int) -> list:
    """상한이 있으면 importance 높은 순으로만 남긴다. 0이면 그대로 (기본)."""
    if not max_guides or len(guides) <= max_guides:
        return guides
    return sorted(guides, key=lambda g: (-(g.get("importance") or 0),
                                         g.get("best_visual_timestamp") or 0))[:max_guides]


def merge_runs(runs: list, max_guides: int) -> dict:
    """여러 분석 결과를 첫 실행의 단계 구조 위로 합친다.

    가이드는 시간으로 단계에 다시 매달고, 같은 순간이 중복되면 버리며,
    마지막에 importance 순으로 상한까지만 남긴다 (상한의 의미를 지킨다).
    """
    merged = dict(runs[0])
    steps = merged.get("steps", [])
    guides = []
    for run in runs:
        for guide in run.get("visual_guides", []):
            timestamp = guide.get("best_visual_timestamp")
            if timestamp is None:
                continue
            guide = dict(guide)
            guide["step_id"] = _step_for(timestamp, steps)
            if any(_same_guide(guide, kept) for kept in guides):
                continue
            guides.append(guide)
    guides.sort(key=lambda g: (-(g.get("importance") or 0),
                               g.get("best_visual_timestamp") or 0))
    merged["visual_guides"] = renumber(trim_guides(guides, max_guides))
    merged["_analysis_passes"] = len(runs)
    return merged


# 긴 영상은 단계가 촘촘히 쪼개지는데 가이드는 4~5개에서 멈춘다 (실측 20편: 10분 이상은
# 상한을 없애도 커버리지 44%→46%로 제자리, 10분 미만은 41%→66%). 전체를 한 번 더 보게
# 해도 같은 곳만 본다 — 그래서 **빈 단계의 시간 구간만** 좁혀서 되묻는다. 단계 구조는
# 그대로 두므로, 구간마다 단계를 새로 만들게 했다가 커버리지가 되레 떨어졌던 방식과 다르다.
FILL_SCHEMA = {
    "type": "object",
    "required": ["guides"],
    "properties": {"guides": {"type": "array", "items": {
        "type": "object",
        "required": ["source_phrase", "phrase", "type", "what_to_show",
                     "best_visual_timestamp", "guide_text", "importance"],
        "properties": {
            "source_phrase": {"type": "string"}, "phrase": {"type": "string"},
            "type": {"enum": ["size", "thickness", "color", "state", "amount",
                              "position", "angle", "action", "texture"]},
            "what_to_show": {"type": "string"},
            "best_visual_timestamp": {"type": "string"},
            "guide_text": {"type": "string"},
            "importance": {"type": "number"},
        }}}},
}

FILL_PROMPT = """이 영상의 한 단계 구간만 봅니다.

단계: {summary}
설명: {detail}
구간: {window}

이 구간에 **글로만 읽으면 기준·정도·위치를 알 수 없는 표현**이 있으면 그 각각을 guides에
담으세요. "한입 크기", "노릇노릇", "이 정도로", "여기에" 같은 것들입니다.
없으면 guides를 빈 배열로 두세요 — 억지로 만들지 않습니다. 단순 절차 설명이나
한 장의 정지 화면으로 확인할 수 없는 조언(힘의 세기, 내부 감각)은 대상이 아닙니다.

best_visual_timestamp는 **영상 시작 기준** MM:SS이며 반드시 이 구간 안이어야 합니다.
사람이 읽는 문자열은 {language} 언어로 씁니다. JSON만 출력합니다."""

# 되묻기 호출 상한 — 25분 영상은 빈 단계가 17개까지 나온다. 긴 단계부터 채운다.
MAX_FILL_CALLS = 8


# 되묻기가 만들어내는 쓰레기를 출력 검사로 거른다. 프롬프트에 금지 목록을 넣는 방법은
# 실측에서 실패했다 — 수확률은 올랐지만(57→67%) 진짜 가이드도 같이 줄어 커버리지가
# 69%→53%로 떨어졌고, source_phrase에 타임스탬프를 넣는 새 결함까지 생겼다.
# 아래 규칙은 채점자 두 명(서로 kappa 0.53으로 갈리는)의 라벨 모두에서
# 쓰레기 12개를 잡고 진짜는 0개도 지우지 않았다.
_TIMESTAMP_ONLY = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$")
_NUMERIC_SPEC = re.compile(
    r"\d+\s*(gram(s)?|g\b|kg|mg|ml|cm|mm|inch(es)?|인치|그램|밀리|%)", re.I)
_NOT_IN_A_STILL = re.compile(
    r"천천히|세게|살살|너무 강하|토크|매일|이틀에|두 번|번 연속|번갈아"
    r"|slowly|too crazy|torque|every day|every other", re.I)


def unphotographable(guide: dict) -> str:
    """정지 화면으로 보여줄 수 없는 가이드면 이유를, 아니면 빈 문자열."""
    source = (guide.get("source_phrase") or "").strip()
    blob = f"{source} {guide.get('phrase', '')}"
    if _TIMESTAMP_ONLY.match(source):
        return "원문이 타임스탬프"
    if _NUMERIC_SPEC.search(source):
        return "이미 수치로 정확함"
    if _NOT_IN_A_STILL.search(blob):
        return "속도·힘·횟수는 한 장으로 못 보여줌"
    return ""


def empty_steps(data: dict) -> list:
    """가이드가 하나도 안 붙은 단계를, 길이가 긴 순서로."""
    covered = {g.get("step_id") for g in data.get("visual_guides", [])}
    gaps = [s for s in data.get("steps", []) if s["id"] not in covered
            and s.get("t_start") is not None and s.get("t_end") is not None
            and s["t_end"] > s["t_start"]]
    return sorted(gaps, key=lambda s: s["t_start"] - s["t_end"])


def fill_empty_steps(data: dict, url: str, model: str, key: str,
                     language: str, limit: int = MAX_FILL_CALLS) -> int:
    """빈 단계의 구간만 다시 보게 해 가이드를 채운다. 추가된 개수를 돌려준다."""
    gaps = empty_steps(data)[:limit]
    guides = data.setdefault("visual_guides", [])
    added = 0
    skipped = []
    for step in gaps:
        window = f"{hms(step['t_start'])}~{hms(step['t_end'])}"
        parts = [
            {"file_data": {"file_uri": url},
             "video_metadata": {"start_offset": {"seconds": step["t_start"]},
                                "end_offset": {"seconds": step["t_end"]}}},
            {"text": FILL_PROMPT.format(summary=step.get("summary", ""),
                                        detail=step.get("detail", ""),
                                        window=window, language=language)},
        ]
        try:
            found = generate_json(parts, model, key, FILL_SCHEMA).get("guides", [])
        except (RateLimitError, RuntimeError):
            break                      # 남은 구간은 포기하고 지금까지 채운 것만 쓴다
        for guide in found:
            timestamp = mmss_to_sec(guide.get("best_visual_timestamp"))
            if timestamp is None or not step["t_start"] <= timestamp <= step["t_end"]:
                continue               # 구간 밖 응답은 버린다 (모델이 종종 벗어난다)
            guide["best_visual_timestamp"] = timestamp
            guide["step_id"] = step["id"]
            reason = unphotographable(guide)
            if reason:
                skipped.append(f"{step['id']}: {guide.get('phrase','')} ({reason})")
                continue
            # 기존 id가 연속이라는 보장이 없다 (trim 뒤 vg-1, vg-4, vg-5) — 빈 번호를 찾는다
            taken = {g.get("id") for g in guides}
            nth = len(guides) + 1
            while f"vg-{nth}" in taken:
                nth += 1
            guide["id"] = f"vg-{nth}"
            guides.append(guide)
            added += 1
    if skipped:
        print(f"  사진으로 못 보여줄 가이드 {len(skipped)}개 걸러냄")
    return added


def call_gemini(url: str, prompt: str, model: str, key: str,
                schema: dict, retries: int = 2) -> dict:
    return generate_json(
        [{"file_data": {"file_uri": url}}, {"text": prompt}],
        model, key, schema, retries)


def normalize(data: dict) -> dict:
    from .contract import CONTRACT_VERSION
    data.setdefault("_contract_version", CONTRACT_VERSION)
    normalization_warnings = []
    for step in data.get("steps", []):
        step["t_start"] = mmss_to_sec(step.get("t_start"))
        step["t_end"] = mmss_to_sec(step.get("t_end"))
        step.pop("ambiguity", None)
    for index, guide in enumerate(data.get("visual_guides", [])):
        guide["best_visual_timestamp"] = mmss_to_sec(
            guide.get("best_visual_timestamp"))
        if not guide.get("source_phrase") and guide.get("phrase"):
            guide["source_phrase"] = guide["phrase"]
            normalization_warnings.append(
                f"{guide.get('id', index)}: source_phrase를 phrase로 보완")
        if guide.get("importance") is None:
            guide["importance"] = max(0.5, 1.0 - index * 0.1)
            normalization_warnings.append(
                f"{guide.get('id', index)}: importance 자동 보완")
        guide_type = guide.get("type")
        if guide_type in TYPE_ALIASES:
            guide["type"] = TYPE_ALIASES[guide_type]
            normalization_warnings.append(
                f"{guide.get('id', index)}: type {guide_type}→{guide['type']}")
    if normalization_warnings:
        data["_normalization_warnings"] = normalization_warnings
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--model", default="gemini-flash-lite-latest")
    ap.add_argument("--profile", default="generic", help="분석 프로파일 (generic|recipe|...)")
    ap.add_argument(
        "--language",
        default=os.environ.get("STEPKEEPER_LANGUAGE", "ko"),
        help="사용자 프로파일 출력 언어(BCP-47, 예: ko, en, ja)")
    ap.add_argument("--max-guides", type=int, default=0,
                    help="시각 가이드 상한. 0이면 무제한(기본) — 애매한 표현마다 모두 만든다. "
                         "문서를 짧게 유지하고 싶을 때만 값을 준다")
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 재분석")
    ap.add_argument("--fill-gaps", action="store_true",
                    help="가이드가 없는 단계의 구간만 다시 보게 해 채운다. "
                         "긴 영상에서 단계는 쪼개지는데 가이드가 안 늘어나는 문제용")
    ap.add_argument("--passes", type=int, default=1,
                    help="분석 반복 횟수. 2 이상이면 실행마다 다르게 나오는 "
                         "가이드를 합쳐 더 촘촘한 문서를 만든다 (호출 비용 비례)")
    args = ap.parse_args()
    if args.max_guides < 0:
        ap.error("--max-guides는 0 이상이어야 합니다.")
    if args.passes < 1:
        ap.error("--passes는 1 이상이어야 합니다.")

    vid = video_id(args.url)
    out_file = analysis_file(data_root(), vid, args.profile, args.language)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    duration = fetch_duration(args.url)
    print(f"영상 길이: {hms(duration)} ({duration}s)")

    if out_file.exists() and not args.force:
        print(f"[cache] {out_file} 사용 (재분석은 --force)")
        data = json.loads(out_file.read_text(encoding="utf-8"))
        if data.get("_max_visual_guides") != args.max_guides:
            sys.exit(
                f"캐시의 max-guides={data.get('_max_visual_guides')}가 "
                f"요청값 {args.max_guides}와 다릅니다. --force로 재분석하세요.")
        if data.get("_model") and data["_model"] != args.model:
            sys.exit(
                f"캐시 모델 {data['_model']}이 요청 모델 {args.model}과 다릅니다. "
                "--force로 재분석하세요.")
        errors, _ = validate(data)
        if errors:
            sys.exit("캐시 계약 위반:\n- " + "\n- ".join(errors) +
                     "\n--force로 재분석하세요.")
    else:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            sys.exit("GEMINI_API_KEY 환경변수가 없습니다.")
        try:
            prompt = load_prompt(
                args.profile, hms(duration), args.language, args.max_guides)
            schema = load_schema(args.profile)
        except UnknownProfileError as error:
            sys.exit(str(error))
        print(f"[1/2] Gemini({args.model}) 영상 분석 중... (수십 초~수 분)"
              + (f" x{args.passes}회" if args.passes > 1 else ""))
        try:
            runs = []
            for attempt in range(args.passes):
                if attempt:
                    print(f"  {attempt + 1}회차 분석 (합집합으로 가이드를 늘립니다)")
                runs.append(normalize(call_gemini(
                    args.url, prompt, args.model, key, schema)))
            data = (merge_runs(runs, args.max_guides) if len(runs) > 1
                    else dict(runs[0], visual_guides=trim_guides(
                        runs[0].get("visual_guides", []), args.max_guides)))
        except RateLimitError as error:
            print("Gemini 무료 티어/속도 한도에 도달했습니다.")
            print(str(error))
            sys.exit(75)
        if args.fill_gaps:
            gaps = len(empty_steps(data))
            added = fill_empty_steps(data, args.url, args.model, key, args.language)
            if added:
                # 채운 뒤 상한을 다시 적용하지 않으면 계약을 우리가 깨고 전체 실행을 버린다
                data["visual_guides"] = renumber(
                    trim_guides(data["visual_guides"], args.max_guides))
            # 되묻기는 정지 화면으로 못 보여줄 것(속도·힘·횟수)이나 텍스트가 이미
            # 정확한 것(수치·규격)까지 만들어낸다 — 라벨 82건 기준 43%가 그런 것이었다.
            kept, junk = drop_junk(data["visual_guides"])
            data["visual_guides"] = renumber(kept)
            print(f"  빈 단계 {gaps}개 중 {min(gaps, MAX_FILL_CALLS)}개를 다시 봐서 "
                  f"가이드 {added}개 추가"
                  + (f", 그중 {len(junk)}개는 걸러냄" if junk else ""))
            for reason in junk:
                print(f"    걸러냄 — {reason}")
            data["_gap_fill"] = {"empty_steps": gaps, "added": added,
                                 "filtered": len(junk)}
        data["_duration"] = duration
        data["_asset_digest"] = asset_digest(args.profile)
        data["_profile"] = args.profile
        data["_output_language"] = args.language
        data["_max_visual_guides"] = args.max_guides
        data["_model"] = args.model
        data["visual_guides"] = order_for_reading(data["visual_guides"])
        errors, warnings = validate(data)
        if errors:
            sys.exit("분석 결과 계약 위반:\n- " + "\n- ".join(errors))
        for warning in warnings:
            print(f"[경고] {warning}")
        out_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[2/2] 저장: {out_file}\n")

    print(f"== {data.get('title', '?')} ==")
    print(f"준비물 {len(data.get('materials') or data.get('ingredients') or [])}종 / 단계 {len(data.get('steps', []))}개\n")

    guides = data.get("visual_guides", [])
    guides_by_step = {}
    for guide in guides:
        guides_by_step.setdefault(guide.get("step_id"), []).append(guide)

    bad = 0
    for s in data.get("steps", []):
        step_guides = guides_by_step.get(s.get("id"), [])
        mark = f" [시각 가이드 {len(step_guides)}]" if step_guides else ""
        print(f"  {s['id']}. [{hms(s['t_start'])}-{hms(s['t_end'])}] {s['summary']}{mark}")
        for guide in step_guides:
            ts = guide.get("best_visual_timestamp")
            print(f"       {guide['id']}: '{guide['phrase']}' ({guide['type']}, 중요도 {guide['importance']})")
            print(f"       가이드: {guide['guide_text']}")
            if ts is None:
                print("       장면: (적합한 장면 없음 -> 텍스트 가이드만)")
            elif ts >= duration:
                bad += 1
                print(f"       장면: {hms(ts)} [범위밖! 영상 길이 {hms(duration)}]")
            else:
                print(f"       검증 링크: https://youtu.be/{vid}?t={ts}  ({hms(ts)})")
        print()

    print(f"시각 가이드 {len(guides)}개 (범위 밖 {bad}개).")
    print("통과 기준: 범위 밖 0개 + 상위 3개 후보 중 적합한 장면 포함률 90% 이상.")


if __name__ == "__main__":
    main()
