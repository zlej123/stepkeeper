#!/usr/bin/env python3
"""Extract three candidate frames for each independent visual guide.

Usage:
    python -m stepkeeper.capture VIDEO_ID --profile generic --language ko

picker.html lets a person choose one candidate per guide (or mark all
unsuitable) and download picks.json / semantic-evaluation.json.
When picks.json already exists (e.g. written by stepkeeper.autopick), the picker
pre-selects those picks and the evaluation download records agree/disagree
per guide — that file doubles as the auto-pick feedback record.
"""
import argparse
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from .common import analysis_file, data_root, frames_dir, hms

sys.stdout.reconfigure(encoding="utf-8")

SLOTS = ("before", "center", "after")


def sh(*args: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"실패: {' '.join(args[:3])}...\n{result.stderr[-2000:]}")


def playable(path: Path) -> bool:
    """프레임이 **실제로 디코드되는지** 확인 (다운로드·캐시 검증 공용).

    스트림 메타만 보면 안 된다: 실측된 깨진 파일은 헤더가 멀쩡해서 ffprobe가
    "h264, 122초"를 정상 보고했지만 (48KB뿐이라) 프레임 데이터가 없었다.
    한 장 디코드가 유일하게 확실한 판정이다.
    """
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True)
    return result.returncode == 0 and not result.stderr.strip()


# 화면 녹화 영상은 정보가 작은 UI 텍스트에 담겨 있어 480p에서 판독이 안 된다
# (실측: Figma 재생 버튼·플러그인 검색어·엑셀 셀이 세 후보 모두 못 쓰는 후보가 됐다).
# 다행히 화면 녹화는 정적이라 압축이 잘 먹어, 고해상도로 받아도 파일이 크게 늘지 않는다.
DEFAULT_CAPTURE_HEIGHT = 480
SCREEN_CAPTURE_HEIGHT = 1080
# 카테고리는 모델이 출력 언어로 쓴다 — 언어별 표기를 모두 적어야 한다.
SCREEN_CATEGORIES = ("소프트웨어", "software", "ソフトウェア")


def capture_height(data: dict | None = None) -> int:
    """받을 영상 높이. STEPKEEPER_CAPTURE_HEIGHT가 있으면 그 값이 우선한다."""
    override = os.environ.get("STEPKEEPER_CAPTURE_HEIGHT")
    if override and override.isdigit():
        return int(override)
    category = ((data or {}).get("category") or "").strip().lower()
    if any(word in category for word in SCREEN_CATEGORIES):
        return SCREEN_CAPTURE_HEIGHT
    return DEFAULT_CAPTURE_HEIGHT


def ensure_video(vid: str, height: int = DEFAULT_CAPTURE_HEIGHT) -> Path:
    """영상을 받아 두고 재사용한다. **받은 파일이 재생 가능한지 확인한다.**

    yt-dlp는 포맷을 못 가져와도 exit 0으로 끝나며 쓸 수 없는 조각 파일을 남길 수 있다
    (실측: 48KB 파일 → ffmpeg "Invalid data found"). 그 파일이 캐시로 남으면 이후 실행이
    영원히 같은 에러로 죽는다 — 검증에 실패하면 지우고 원인을 알려주며 멈춘다.

    해상도가 다르면 캐시를 재사용하지 않는다 — 파일명에 높이를 넣어 구분한다.
    """
    root = data_root() / "work"
    mp4 = root / (f"{vid}.mp4" if height == DEFAULT_CAPTURE_HEIGHT
                  else f"{vid}.{height}p.mp4")
    if mp4.exists() and not playable(mp4):
        print("[1/3] 캐시된 영상이 손상됨 — 지우고 다시 받습니다")
        mp4.unlink()
    if not mp4.exists():
        print(f"[1/3] {height}p 영상 다운로드...")
        sh(sys.executable, "-m", "yt_dlp", "-f",
           f"bv*[height<={height}]+ba/b[height<={height}]/b",
           "--merge-output-format", "mp4", "-o", str(mp4),
           f"https://www.youtube.com/watch?v={vid}")
        if not mp4.exists() or not playable(mp4):
            size = mp4.stat().st_size if mp4.exists() else 0
            mp4.unlink(missing_ok=True)
            sys.exit(
                f"영상을 받지 못했습니다 ({vid}, {size}바이트로 중단). yt-dlp가 포맷을 "
                "가져오지 못했을 수 있습니다 — 최신 yt-dlp로 올리거나, YouTube 추출에 "
                "필요한 JS 런타임(deno 등)을 설치한 뒤 다시 시도하세요.\n"
                "  pip install -U yt-dlp   /   brew install deno")
    else:
        print("[1/3] 영상 캐시 사용")
    return mp4


# 후보 간격 상한(초). 동작은 같은 동작 안에 머물도록 더 촘촘히 본다.
ACTION_CANDIDATE_SPREAD = 1
DEFAULT_CANDIDATE_SPREAD = 2


def candidate_times(step: dict, guide: dict, duration: int):
    """center 주변에서 세 후보를 뽑는다 (스텝 경계가 아니라).

    예전에는 before/after를 스텝 경계(t_start-1, t_end+1)에 뒀는데, 긴 스텝에서는 그 둘이
    **다른 주제**를 찍는다. 실측 사례: 19초짜리 스텝의 가이드에서 후보가 18·31·39초로 잡혔고,
    18초는 이전 섹션, 39초는 다음 섹션이었다. 정작 가이드가 요구한 동작은 26~29초에 있었는데
    세 장 중 어디에도 없어서, 사람이 골라도 실패할 선택지가 됐다.

    동작 가이드는 center±1초, 상태·위치·각도 등은 최대 ±2초로 제한한다. 실측 리뷰에서
    ±4초 후보가 결과·준비·다음 동작으로 갈라져 같은 가이드의 비교가 아니게 된 문제를 막는다.
    """
    center = guide["best_visual_timestamp"]
    last = max(0, duration - 1)
    limit = (ACTION_CANDIDATE_SPREAD if guide.get("type") == "action"
             else DEFAULT_CANDIDATE_SPREAD)
    if step:
        length = max(0, step.get("t_end", center) - step.get("t_start", center))
        spread = max(1, min(limit, length // 4))
    else:
        spread = limit
    before = max(0, center - spread)
    after = min(last, center + spread)
    # 후보가 스텝 경계를 넘으면 이전/다음 단계의 장면이 들어온다 (외부 리뷰 P2-3:
    # 스텝이 10초에 시작하고 center=10이면 before=9는 이전 단계다). 단 center 자체가
    # 스텝 범위 밖이면 스텝 정보를 불신하고 클램프하지 않는다 — 모델이 준 center를
    # 경계로 끌어오면 "가장 잘 보이는 순간"에서 멀어진다.
    if step and "t_start" in step and "t_end" in step \
            and step["t_start"] <= center <= step["t_end"]:
        before = max(before, step["t_start"])
        after = min(after, step["t_end"])
    return dict(zip(SLOTS, (before, center, after)))


# ── 적응 후보 탐색 ────────────────────────────────────────────────────────────
# 분석이 찍은 center ±1~2초의 고정 창은 분석 타임스탬프가 빗나가면 복구가 불가능하다.
# 최난도 12건 실측: step 전체를 거칠게 훑고 최적 지점을 재탐색하니 확신 오판 4건이
# 전부 해소됐다(진짜 프레임 3 + 자연 none 1), none 6건은 전부 옳았다. 프롬프트
# 수정 4연속 실패 후, 모델을 설득하는 대신 후보 생성 구조를 바꾼 것이 통했다.
SEARCH_SCHEMA = {
    "type": "object",
    "required": ["found"],
    "properties": {"found": {"type": "boolean"}, "t": {"type": "number"},
                   "why": {"type": "string"}},
}
SEARCH_PROMPT = """아래는 한 단계 구간에서 뽑은 프레임들이며 각 프레임 앞에 시각(초)이 붙어 있습니다.
'보여야 할 것'이 실제로 알아볼 수 있게 보이는 프레임이 있으면 found=true와 그 시각 t를,
없으면 found=false를 답하세요. 억지로 고르지 않습니다. JSON만 출력합니다."""

SEARCH_COARSE_LIMIT = 48       # 한 요청에 담는 최대 프레임 수 (토큰 상한)
SEARCH_WIDTH = 640             # 탐색용 프레임 폭 — 판정에는 충분하고 토큰은 절반


def search_window(step: dict, guide: dict, duration: int) -> tuple:
    """탐색 범위 — step 경계, 시간이 없으면 center ±30초."""
    center = guide["best_visual_timestamp"]
    start, end = step.get("t_start"), step.get("t_end")
    if start is None or end is None or end <= start:
        start, end = center - 30, center + 30
    return max(0, start), min(max(0, duration - 1), end)


def _extract_frame(mp4: Path, timestamp: float, out: Path, width: int = 0) -> bool:
    scale = ["-vf", f"scale={width}:-2"] if width else []
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(round(timestamp, 2)),
         "-i", str(mp4), "-frames:v", "1", "-q:v", "3", *scale, str(out)],
        capture_output=True, text=True)
    return result.returncode == 0 and out.exists()


def search_center(mp4: Path, guide: dict, step: dict, duration: int,
                  model: str, key: str):
    """step 구간을 탐색해 '보여야 할 것'이 보이는 시각을 찾는다.

    반환: 시각(float) — 찾았을 때 / None — 구간에 없다고 판정(자연 none) /
    "error" — 호출 실패(호출자는 고정 창으로 폴백한다).
    """
    import base64
    import tempfile

    from .analyze import RateLimitError, generate_json

    start, end = search_window(step, guide, duration)
    span = max(1.0, end - start)
    interval = max(1.0, span / SEARCH_COARSE_LIMIT)

    def ask(frames):
        parts = [{"text": SEARCH_PROMPT},
                 {"text": f"보여야 할 것: {guide.get('what_to_show', '')}"}]
        for timestamp, path in frames:
            parts.append({"text": f"t={timestamp:.1f}s:"})
            parts.append({"inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(path.read_bytes()).decode()}})
        return generate_json(parts, model, key, SEARCH_SCHEMA)

    with tempfile.TemporaryDirectory() as tmp:
        coarse = []
        timestamp = float(start)
        while timestamp <= end:
            frame = Path(tmp) / f"c{timestamp:.2f}.jpg"
            if _extract_frame(mp4, timestamp, frame, SEARCH_WIDTH):
                coarse.append((timestamp, frame))
            timestamp += interval
        if not coarse:
            return "error"
        try:
            got = ask(coarse)
            if not got.get("found"):
                return None
            best = float(got["t"])
            fine = []
            for delta in (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0):
                frame = Path(tmp) / f"f{delta:+.2f}.jpg"
                if _extract_frame(mp4, max(0.0, best + delta), frame, SEARCH_WIDTH):
                    fine.append((best + delta, frame))
            if fine:
                refined = ask(fine)
                if refined.get("found"):
                    best = float(refined["t"])
            return max(0.0, best)
        except RateLimitError:
            raise
        except Exception:
            return "error"


def sync_candidate_meta(out: Path, times: dict) -> bool:
    """candidates.json에 후보별 타임스탬프를 기록하고, 달라졌으면 선택을 무효화한다.

    창 규칙이나 분석이 바뀌면 같은 "vg-1_center.jpg" 파일명이 전혀 다른 장면을
    가리키게 된다 — 원인이 무엇이든 결국 타임스탬프 변화로 나타나므로, 이 파일
    하나와 비교해 어긋나면 picks.json/picks-meta.json을 지운다.
    반환값: 기존 선택을 무효화했으면 True.
    """
    meta = out / "candidates.json"
    previous = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None
    meta.write_text(json.dumps(times, ensure_ascii=False, indent=2), encoding="utf-8")
    # 기록이 없으면(candidates.json 도입 전 데이터) 선택이 지금 후보와 맞는지 검증할
    # 방법이 없다 — 맞다고 가정하지 않고 무효화한다 (fail-closed).
    if previous == times:
        return False
    invalidated = False
    for stale in (out / "picks.json", out / "picks-meta.json"):
        if stale.exists():
            stale.unlink()
            invalidated = True
    return invalidated


def build_picker(vid: str, profile: str, language: str) -> Path:
    """(Re)generate picker.html from analysis + frames on disk.

    If picks.json exists, its choices are pre-selected and marked as AI picks
    so the evaluation download becomes a feedback record.
    """
    source = analysis_file(data_root(), vid, profile, language)
    data = json.loads(source.read_text(encoding="utf-8"))
    out = frames_dir(data_root(), vid, profile, language)

    picks_file = out / "picks.json"
    ai_picks = {}
    if picks_file.exists():
        ai_picks = {key: value for key, value in
                    json.loads(picks_file.read_text(encoding="utf-8")).items()
                    if not key.startswith("_")}

    steps = {step["id"]: step for step in data.get("steps", [])}
    guides = [guide for guide in data.get("visual_guides", [])
              if guide.get("best_visual_timestamp") is not None]

    rows = []
    guide_ids = []
    for guide in guides:
        guide_id = guide["id"]
        step = steps.get(guide["step_id"], {})
        # 적응 탐색이 "구간에 없음"으로 판정한 가이드는 프레임이 없다 — 고를 것도,
        # 평가할 것도 없으므로 안내만 표시한다 (문서에는 링크가 들어간다).
        if not (out / f"{guide_id}_center.jpg").exists():
            rows.append(
                f'<section data-guide="{html.escape(guide_id)}">'
                f'<h2>{html.escape(guide_id)} · 단계 {guide["step_id"]}: '
                f'{html.escape(step.get("summary", ""))}</h2>'
                f'<p><b>표시:</b> {html.escape(guide.get("phrase", ""))}</p>'
                f'<p class="none-note">탐색 결과 이 구간에 해당 장면이 없습니다 — '
                f'문서에는 타임스탬프 링크가 들어갑니다.</p></section>')
            continue
        guide_ids.append(guide_id)
        times = candidate_times(step, guide, data.get("_duration", 0))
        preset = ai_picks.get(guide_id)
        cells = "".join(
            f'<label class="cell"><input type="radio" name="{guide_id}" value="{slot}"'
            f'{" checked" if preset == slot else ""}>'
            f'<img src="{guide_id}_{slot}.jpg"><span>{hms(times[slot])} ({slot})'
            f'{" · AI 선택" if preset == slot else ""}</span></label>'
            for slot in SLOTS)
        cells += (
            f'<label class="cell none"><input type="radio" name="{guide_id}" value="none"'
            f'{" checked" if preset == "none" else ""}>'
            f'<span class="none-box">세 장 모두 부적합<br>링크만 사용'
            f'{"<br>· AI 선택" if preset == "none" else ""}</span></label>')
        rows.append(
            f'<section data-guide="{html.escape(guide_id)}">'
            f'<h2>{html.escape(guide_id)} · 단계 {guide["step_id"]}: '
            f'{html.escape(step.get("summary", ""))}</h2>'
            f'<p><b>원문:</b> {html.escape(guide["source_phrase"])} &nbsp; '
            f'<b>표시:</b> {html.escape(guide["phrase"])}</p>'
            f'<p><b>판정 기준:</b> {html.escape(guide["what_to_show"])}<br>'
            f'<b>가이드:</b> {html.escape(guide["guide_text"])}</p>'
            f'<div class="row">{cells}</div></section>')

    metadata = json.dumps({
        "video_id": vid,
        "profile": profile,
        "language": language,
        "guide_ids": guide_ids,
        "ai_picks": ai_picks,
    }, ensure_ascii=False)
    intro = ("AI가 고른 장면이 미리 선택되어 있습니다. 틀린 것만 바꾼 뒤 "
             "피드백(semantic-evaluation.json)을 내려받아 주세요."
             if ai_picks else
             "각 가이드에서 의미를 가장 잘 보여주는 장면 하나를 선택하세요. 자동 선택은 없습니다.")
    page = f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(data['title'])} — 장면 선택</title>
<style>
 body{{font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:1200px;margin:24px auto;padding:0 12px}}
 .row{{display:flex;gap:12px;align-items:stretch}} .cell{{flex:1;text-align:center;cursor:pointer}}
 .cell img{{width:100%;border:3px solid #ddd;border-radius:8px;box-sizing:border-box}}
 .cell input{{position:absolute;opacity:0}} .cell input:checked+img{{border-color:#e5484d}}
 .cell span{{font-size:13px;color:#666}} .none-box{{display:flex;height:100%;min-height:150px;border:3px solid #ddd;
 border-radius:8px;align-items:center;justify-content:center;box-sizing:border-box}}
 .none input:checked+.none-box{{border-color:#e5484d;background:#fff1f1}}
 section{{margin-bottom:42px}} button{{padding:12px 18px;margin:8px;font-size:15px}}
</style>
<h1>{html.escape(data['title'])}</h1>
<p>{intro}</p>
{"".join(rows)}
<div><button onclick="downloadPicks()">picks.json 내려받기</button>
<button onclick="downloadEvaluation()">semantic-evaluation.json 내려받기 (피드백)</button></div>
<script>
const META={metadata};
function selections(){{
  const result={{}};
  for(const id of META.guide_ids){{
    const selected=document.querySelector(`input[name="${{id}}"]:checked`);
    if(selected) result[id]=selected.value;
  }}
  return result;
}}
function download(name,data){{
  const blob=new Blob([JSON.stringify(data,null,2)],{{type:'application/json'}});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name; a.click();
  URL.revokeObjectURL(a.href);
}}
function downloadPicks(){{download('picks.json',selections());}}
function downloadEvaluation(){{
  const selected=selections();
  const guides=META.guide_ids.map(id=>{{
    const slot=selected[id]||null;
    const ai=META.ai_picks[id]||null;
    return {{guide_id:id,
      selected_slot:slot&&slot!=='none'?slot:null,
      candidate_hit:Boolean(slot&&slot!=='none'),
      reviewed:Boolean(slot),
      ai_slot:ai,
      agree:ai?ai===slot:null}};
  }});
  download('semantic-evaluation.json',{{video_id:META.video_id,profile:META.profile,
    language:META.language,ai_reviewed:Object.keys(META.ai_picks).length>0,guides}});
}}
</script>"""
    picker = out / "picker.html"
    picker.write_text(page, encoding="utf-8")
    return picker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--profile", default="generic")
    ap.add_argument("--language", default="ko")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--no-search", action="store_true",
                    help="적응 탐색 없이 분석 타임스탬프 ±1~2초 고정 창만 사용")
    args = ap.parse_args()

    vid = args.video_id
    source = analysis_file(data_root(), vid, args.profile, args.language)
    if not source.exists():
        sys.exit(f"분석 결과 없음: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    mp4 = ensure_video(vid, capture_height(data))

    out = frames_dir(data_root(), vid, args.profile, args.language)
    out.mkdir(parents=True, exist_ok=True)
    # Refresh candidate JPEGs only. Keep picks.json / picks-meta.json so a
    # re-capture does not wipe AI or human selections (picker re-reads them) —
    # unless the candidate timestamps changed, in which case the same "center"
    # filename would point at a different scene and old picks become lies.
    for stale in list(out.glob("vg-*.jpg")) + [out / "contact-sheet.jpg"]:
        if stale.exists():
            stale.unlink()

    steps = {step["id"]: step for step in data.get("steps", [])}
    guides = [guide for guide in data.get("visual_guides", [])
              if guide.get("best_visual_timestamp") is not None]
    duration = data.get("_duration", 0)

    # 기본 탐색: 분석 타임스탬프를 믿지 않고 step 구간에서 실제로 보이는 시각을 찾는다.
    # 키가 없거나 --no-search면 종전의 고정 창을 쓴다 (오프라인 동작 보존).
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    searching = bool(key) and not args.no_search
    times = {}
    searched_none = []
    from .analyze import RateLimitError
    for guide in guides:
        step = steps.get(guide["step_id"], {})
        if searching:
            try:
                found = search_center(mp4, guide, step, duration, args.model, key)
            except RateLimitError as error:
                print("Gemini 한도 도달:", str(error)[-200:])
                sys.exit(75)
            if isinstance(found, float):
                anchored = dict(guide, best_visual_timestamp=int(round(found)))
                times[guide["id"]] = candidate_times(step, anchored, duration)
                continue
            # none이든 호출 실패든 고정 창으로 폴백한다. 탐색의 none을 그대로 믿고
            # 링크로 보내면 안 된다 — 실측: 걸쭉한 면이 476초에 뚜렷한데 같은 48장
            # 재실행에서 none↔발견이 갈렸다(비결정성). 폴백이면 거짓 none은 "옛
            # 동작"이 될 뿐이고, 진짜 없는 경우는 하류 autopick이 기권한다.
            if found is None:
                searched_none.append(guide["id"])
                print(f"  {guide['id']}: 탐색이 장면을 못 찾음 — 고정 창으로 폴백")
            else:
                print(f"  {guide['id']}: 탐색 호출 실패 — 고정 창으로 폴백")
        times[guide["id"]] = candidate_times(step, guide, duration)

    if sync_candidate_meta(out, times):
        print("후보 타임스탬프가 달라져 기존 선택(picks)을 무효화했습니다 — 다시 선택하세요.")

    print(f"[2/3] 시각 가이드 {len(times)}개 x {len(SLOTS)}장 프레임 추출...")
    for guide_id, slots in times.items():
        for slot, timestamp in slots.items():
            sh("ffmpeg", "-y", "-loglevel", "error", "-ss", str(timestamp),
               "-i", str(mp4), "-frames:v", "1", "-q:v", "3",
               "-strict", "unofficial", str(out / f"{guide_id}_{slot}.jpg"))

    print("[3/3] picker.html 생성...")
    picker = build_picker(vid, args.profile, args.language)
    print(f"완료: {picker}")
    print("자동 선택 없음: picker.html에서 선택하거나 stepkeeper.autopick을 실행하세요.")


if __name__ == "__main__":
    main()
