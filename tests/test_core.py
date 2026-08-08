import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stepkeeper import analyze, capture
from stepkeeper import export as exporter
from stepkeeper import render as renderer
from stepkeeper.contract import validate
from stepkeeper.common import analysis_file, frames_dir, hms, output_dir, video_id


class FixtureCorpusTests(unittest.TestCase):
    def test_en_output_smoke_suite_is_wired(self):
        fixtures = json.loads(
            (ROOT / "tests" / "fixtures" / "urls.json").read_text(encoding="utf-8"))
        suite = fixtures["en_output"]
        self.assertEqual("smoke", suite.get("suite"))
        self.assertEqual("en", suite.get("language"))
        videos = suite["videos"]
        # 영어 출력이 주요 배포 대상이 되면서 4편 → 10편으로 늘렸다 (도메인 6개 커버).
        # 상한은 배치 1회 실행의 무료 티어 사용량을 감당할 수 있는 선.
        self.assertTrue(6 <= len(videos) <= 12, len(videos))
        profiles = {video.get("profile", suite["profile"]) for video in videos}
        self.assertIn("recipe", profiles)
        self.assertIn("generic", profiles)
        for video in videos:
            self.assertEqual("en", video.get("language", suite["language"]))
            self.assertEqual("en", video["strata"]["source_language"])

    def test_fixture_variants_may_share_video_id_across_languages(self):
        """Same YouTube id may appear for ko domain coverage and en_output smoke."""
        fixtures = json.loads(
            (ROOT / "tests" / "fixtures" / "urls.json").read_text(encoding="utf-8"))
        keys = []
        for domain, config in fixtures.items():
            if domain.startswith("_") or not isinstance(config, dict):
                continue
            default_profile = config.get("profile", "generic")
            default_language = config.get("language", "ko")
            for video in config.get("videos", []):
                match = re.search(
                    r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", video["url"])
                self.assertIsNotNone(match, video["url"])
                keys.append((
                    match.group(1),
                    video.get("profile", default_profile),
                    video.get("language", default_language),
                ))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(any(language == "en" for _, _, language in keys))


class CoreContractTests(unittest.TestCase):
    def valid_data(self):
        return {
            "title": "테스트 가이드",
            "summary": "요약",
            "category": "공예",
            "materials": [{"name": "종이", "amount": "1장"}],
            "steps": [
                {"id": 1, "summary": "접기", "detail": "종이를 접는다.",
                 "t_start": 0, "t_end": 10},
            ],
            "visual_guides": [
                {"id": "vg-1", "step_id": 1, "source_phrase": "fold it",
                 "phrase": "종이를 반으로 접기", "type": "action",
                 "what_to_show": "두 모서리가 만나는 장면",
                 "best_visual_timestamp": 8,
                 "guide_text": "두 모서리가 정확히 겹치도록 반으로 접는다.",
                 "importance": 0.9},
            ],
            "_duration": 20,
            "_profile": "generic",
            "_output_language": "ko",
            "_max_visual_guides": 5,
        }

    def test_valid_independent_visual_guides(self):
        errors, _ = validate(self.valid_data())
        self.assertEqual([], errors)

    def test_legacy_step_ambiguity_is_rejected(self):
        data = self.valid_data()
        data["steps"][0]["ambiguity"] = None
        errors, _ = validate(data)
        self.assertTrue(any("legacy ambiguity" in error for error in errors))

    def test_unknown_step_reference_is_rejected(self):
        data = self.valid_data()
        data["visual_guides"][0]["step_id"] = 999
        errors, _ = validate(data)
        self.assertTrue(any("없는 step_id" in error for error in errors))

    def test_normalize_visual_guide_timestamp(self):
        data = self.valid_data()
        data["steps"][0]["t_start"] = "0:01"
        data["steps"][0]["t_end"] = "1:02"
        data["visual_guides"][0]["best_visual_timestamp"] = "0:08"
        normalized = analyze.normalize(data)
        self.assertEqual(1, normalized["steps"][0]["t_start"])
        self.assertEqual(62, normalized["steps"][0]["t_end"])
        self.assertEqual(8, normalized["visual_guides"][0]["best_visual_timestamp"])

    def test_normalize_repairs_common_model_variants(self):
        data = self.valid_data()
        guide = data["visual_guides"][0]
        guide.pop("source_phrase")
        guide.pop("importance")
        guide["type"] = "direction"
        normalized = analyze.normalize(data)
        repaired = normalized["visual_guides"][0]
        self.assertEqual(guide["phrase"], repaired["source_phrase"])
        self.assertEqual("position", repaired["type"])
        self.assertIn("_normalization_warnings", normalized)

    def test_normalize_keeps_zero_importance(self):
        data = self.valid_data()
        data["visual_guides"][0]["importance"] = 0.0
        normalized = analyze.normalize(data)
        self.assertEqual(0.0, normalized["visual_guides"][0]["importance"])
        self.assertNotIn("_normalization_warnings", normalized)

    def test_asset_digest_tracks_prompt_changes(self):
        d1 = analyze.asset_digest("generic")
        self.assertEqual(12, len(d1))
        self.assertEqual(d1, analyze.asset_digest("generic"))       # 결정적
        self.assertNotEqual(d1, analyze.asset_digest("recipe"))     # 프로파일별로 다름

    def test_normalize_stamps_contract_version(self):
        from stepkeeper.contract import CONTRACT_VERSION
        normalized = analyze.normalize(self.valid_data())
        self.assertEqual(CONTRACT_VERSION, normalized["_contract_version"])
        # 이미 스탬프된 데이터(캐시 재정규화)는 덮어쓰지 않는다
        data = self.valid_data()
        data["_contract_version"] = "0-test"
        self.assertEqual("0-test", analyze.normalize(data)["_contract_version"])

    def test_saved_outputs_carry_the_safety_notice(self):
        """고지는 화면 UI만이 아니라 **저장물 자체**에 남는다 (리뷰 3차 P1-3 잔여).

        파일을 공유받은 사람은 앱 화면을 보지 않는다 — markdown·Notion·PDF 모두에
        고지가 있어야 하고, 무해한 문서는 한 바이트도 달라지지 않아야 한다.
        """
        risky = CoreContractTests().valid_data()
        risky["title"] = "콘센트 전기 배선 교체하기"
        safe = CoreContractTests().valid_data()

        # markdown: 템플릿 섹션
        template = renderer.load_template("generic", "ko")
        body = template.split("\n---\n", 1)[1]
        with tempfile.TemporaryDirectory() as temp:
            images = Path(temp)
            ctx_risky = renderer.build_context("vid00000000", risky, {}, images, images)
            ctx_safe = renderer.build_context("vid00000000", safe, {}, images, images)
            md_risky = renderer.render(body, ctx_risky)
            md_safe = renderer.render(body, ctx_safe)
        self.assertIn("⚠️ **안전이 걸린 주제입니다.**", md_risky)
        self.assertNotIn("⚠️", md_safe)
        # 무해 문서는 고지 도입 전과 같은 모양 (제목 다음 빈 줄 하나 → 요약)
        self.assertIn("## 📋 테스트 가이드\n\n요약", md_safe)

        # Notion: 최상단 callout
        blocks_risky = exporter.build_notion_blocks(risky, "vid00000000", {})
        blocks_safe = exporter.build_notion_blocks(safe, "vid00000000", {})
        self.assertEqual("callout", blocks_risky[0]["type"])
        self.assertIn("안전이 걸린 주제", str(blocks_risky[0]))
        self.assertNotIn("callout", [b["type"] for b in blocks_safe])

    def test_high_risk_domain_warns(self):
        """의료·전기·가스 등 안전 결정 영상은 경고 채널로 표시된다 (외부 리뷰 #10)."""
        data = self.valid_data()
        data["title"] = "콘센트 전기 배선 교체하기"
        errors, warnings = validate(data)
        self.assertEqual([], errors)
        self.assertTrue(any("고위험" in warning for warning in warnings))

        safe = self.valid_data()
        errors, warnings = validate(safe)
        self.assertFalse(any("고위험" in warning for warning in warnings))

    def test_vague_english_guide_text_warns(self):
        data = self.valid_data()
        data["visual_guides"][0]["guide_text"] = "Cook until done, just enough."
        errors, warnings = validate(data)
        self.assertEqual([], errors)
        self.assertTrue(any("막연 표현" in warning for warning in warnings))

    def test_video_id_parses_all_url_forms(self):
        self.assertEqual("GC_Szxdqh2Y", video_id("https://www.youtube.com/watch?v=GC_Szxdqh2Y"))
        self.assertEqual("Ff9BQUkhTZ4", video_id("https://www.youtube.com/shorts/Ff9BQUkhTZ4"))
        self.assertEqual("4ioPBiTWm3M", video_id("https://youtu.be/4ioPBiTWm3M"))
        with self.assertRaises(ValueError):
            video_id("https://example.com/not-a-video")

    def test_hms_formats_minutes_and_hours(self):
        self.assertEqual("0:08", hms(8))
        self.assertEqual("1:02", hms(62))
        self.assertEqual("1:01:05", hms(3665))

    def test_prompt_injects_user_language_and_duration(self):
        prompt = analyze.load_prompt("generic", "6:41", "ja", 7)
        self.assertIn("ja", prompt)
        self.assertIn("6:41", prompt)
        self.assertNotIn("{OUTPUT_LANGUAGE}", prompt)
        # 개수 상한은 더 이상 프롬프트로 걸지 않는다 — 걸면 커버리지가 무너진다(실측 43%↔73%).
        # 상한이 필요하면 분석 뒤 trim_guides로 자른다.
        self.assertNotIn("{MAX_VISUAL_GUIDES}", prompt)
        self.assertNotIn("7개 이하", prompt)

    def test_template_follows_output_language(self):
        """문서 뼈대(라벨·출처 줄)도 --language를 따라야 한다.

        회귀 방지: 예전에는 template.md가 한국어 하드코딩이라 --language en 문서의
        본문만 영어이고 '준비 재료'·'조리 순서'·'기준:'·'출처:'는 한국어로 남았다.
        """
        for profile in ("recipe", "generic"):
            english = renderer.load_template(profile, "en")
            korean = renderer.load_template(profile, "ko")
            self.assertIn("**■ Steps**", english)
            self.assertIn("kept with stepkeeper", english)
            self.assertNotIn("기준:", english)
            self.assertIn("기준:", korean)
            self.assertIn("stepkeeper로 생성", korean)
            # 번역본이 없는 언어는 영어 기본 뼈대로 떨어진다 (한국어로 새지 않는다)
            japanese = renderer.load_template(profile, "ja")
            self.assertIn("**■ 手順**" if profile == "generic" else "**■ 作り方**", japanese)
            self.assertNotIn("기준:", japanese)
            # 번역본이 없는 언어만 영어로 떨어진다
            self.assertEqual(english, renderer.load_template(profile, "de"))
            self.assertEqual(english, renderer.load_template(profile))

    def test_artifact_paths_are_variant_aware(self):
        self.assertIn("generic.ko.json", str(analysis_file(ROOT, "abc", "generic", "ko")))
        self.assertNotEqual(frames_dir(ROOT, "abc", "generic", "ko"),
                            frames_dir(ROOT, "abc", "generic", "en"))
        self.assertNotEqual(output_dir(ROOT, "abc", "recipe", "ko"),
                            output_dir(ROOT, "abc", "generic", "ko"))


class CandidateTimesTests(unittest.TestCase):
    """후보는 스텝 경계 안에 머문다 (외부 리뷰 P2-3)."""

    def guide(self, center, gtype="state"):
        return {"best_visual_timestamp": center, "type": gtype}

    def test_candidates_clamp_to_step_start(self):
        # 스텝이 10초에 시작하고 center=10이면 before=9는 이전 단계의 장면이다
        times = capture.candidate_times(
            {"t_start": 10, "t_end": 30}, self.guide(10), 100)
        self.assertEqual({"before": 10, "center": 10, "after": 12}, times)

    def test_candidates_clamp_to_step_end(self):
        times = capture.candidate_times(
            {"t_start": 0, "t_end": 20}, self.guide(20), 100)
        self.assertEqual({"before": 18, "center": 20, "after": 20}, times)

    def test_center_outside_step_distrusts_step_boundaries(self):
        # 모델이 준 center가 스텝 밖이면 스텝 정보를 불신한다 — 경계로 끌어오면
        # "가장 잘 보이는 순간"에서 멀어진다
        times = capture.candidate_times(
            {"t_start": 10, "t_end": 20}, self.guide(40), 100)
        self.assertEqual({"before": 38, "center": 40, "after": 42}, times)

    def test_action_guides_stay_within_one_second(self):
        times = capture.candidate_times(
            {"t_start": 0, "t_end": 20}, self.guide(10, "action"), 100)
        self.assertEqual({"before": 9, "center": 10, "after": 11}, times)


class FillEmptyStepsTests(unittest.TestCase):
    """긴 영상은 단계가 쪼개지는데 가이드는 4~5개에서 멈춘다 (실측 20편: 10분 이상은
    상한을 없애도 커버 44%→46% 제자리). 빈 단계 구간만 좁혀 되묻는다."""

    def data(self):
        return {"steps": [{"id": 1, "summary": "a", "t_start": 0, "t_end": 60},
                          {"id": 2, "summary": "b", "t_start": 60, "t_end": 300},
                          {"id": 3, "summary": "c", "t_start": 300, "t_end": 330}],
                "visual_guides": [{"id": "vg-1", "step_id": 1,
                                   "best_visual_timestamp": 10}]}

    def test_empty_steps_are_longest_first(self):
        gaps = analyze.empty_steps(self.data())
        self.assertEqual([2, 3], [s["id"] for s in gaps])   # 240초짜리가 먼저

    def test_fill_attaches_guides_to_the_queried_step(self):
        # 각 구간 안쪽 타임스탬프를 돌려주도록 — 구간마다 물어보는 창이 다르다
        answers = iter(["2:30", "5:10"])
        def fake(parts, model, key, schema):
            return {"guides": [{"source_phrase": "이 정도로", "phrase": "반죽 되기",
                                "type": "state", "what_to_show": "천천히 흐르는 상태",
                                "best_visual_timestamp": next(answers),
                                "guide_text": "천천히 흐른다", "importance": 0.7}]}
        data = self.data()
        with patch.object(analyze, "generate_json", side_effect=fake):
            added = analyze.fill_empty_steps(data, "u", "m", "k", "ko")
        self.assertEqual(2, added)                            # 빈 단계 2개 각각에서 1개
        by_step = {g["step_id"]: g["best_visual_timestamp"]
                   for g in data["visual_guides"]}
        self.assertEqual(150, by_step[2])                     # 2:30 → 초, 2단계(60~300)
        self.assertEqual(310, by_step[3])                     # 5:10 → 초, 3단계(300~330)

    def test_out_of_window_answers_are_dropped(self):
        # 모델이 구간을 벗어난 타임스탬프를 주면 프레임을 엉뚱한 데서 뜬다
        found = {"guides": [{"source_phrase": "x", "phrase": "y", "type": "state",
                             "what_to_show": "z", "best_visual_timestamp": "9:00",
                             "guide_text": "w", "importance": 0.5}]}
        data = self.data()
        with patch.object(analyze, "generate_json", return_value=found):
            self.assertEqual(0, analyze.fill_empty_steps(data, "u", "m", "k", "ko"))

    def test_empty_answer_adds_nothing(self):
        # 억지로 만들지 않는다 — 절차만 있는 단계는 비워 두는 게 맞다
        data = self.data()
        with patch.object(analyze, "generate_json", return_value={"guides": []}):
            self.assertEqual(0, analyze.fill_empty_steps(data, "u", "m", "k", "ko"))

    def test_call_limit_is_respected(self):
        data = {"steps": [{"id": i, "summary": "s", "t_start": i * 60,
                           "t_end": i * 60 + 59} for i in range(1, 21)],
                "visual_guides": []}
        calls = []
        def fake(parts, model, key, schema):
            calls.append(1)
            return {"guides": []}
        with patch.object(analyze, "generate_json", side_effect=fake):
            analyze.fill_empty_steps(data, "u", "m", "k", "ko")
        self.assertEqual(analyze.MAX_FILL_CALLS, len(calls))


class GuideCapTests(unittest.TestCase):
    """프롬프트에 개수 상한을 걸면 커버리지가 무너진다 (실측 9편: 상한 5 → 커버리지 43%,
    상한 제거 → 73%; 대조군은 +5%뿐이라 노이즈가 아니다). 상한은 이제 선택 장치다."""

    def test_prompt_no_longer_caps_the_count(self):
        rules = (Path("src/stepkeeper/skill-core/engine/rules.md")
                 .read_text(encoding="utf-8"))
        self.assertNotIn("{MAX_VISUAL_GUIDES}", rules)
        self.assertIn("개수 상한은 없다", rules)

    def test_zero_means_unlimited(self):
        guides = [{"importance": i / 10} for i in range(9)]
        self.assertEqual(9, len(analyze.trim_guides(guides, 0)))

    def test_cap_keeps_the_most_important(self):
        guides = [{"importance": 0.1, "best_visual_timestamp": 1},
                  {"importance": 0.9, "best_visual_timestamp": 2},
                  {"importance": 0.5, "best_visual_timestamp": 3}]
        kept = analyze.trim_guides(guides, 2)
        self.assertEqual([0.9, 0.5], [g["importance"] for g in kept])

    def test_contract_allows_any_count_when_unlimited(self):
        data = CoreContractTests().valid_data()
        data["_max_visual_guides"] = 0
        base = data["visual_guides"][0]
        data["visual_guides"] = [dict(base, id=f"vg-{i}") for i in range(1, 13)]
        errors, _ = validate(data)
        self.assertEqual([], [e for e in errors if "상한" in e])

    def test_contract_still_enforces_an_explicit_cap(self):
        data = CoreContractTests().valid_data()
        data["_max_visual_guides"] = 2
        base = data["visual_guides"][0]
        data["visual_guides"] = [dict(base, id=f"vg-{i}") for i in range(1, 5)]
        errors, _ = validate(data)
        self.assertTrue(any("상한" in e for e in errors))


class UnphotographableTests(unittest.TestCase):
    """되묻기가 만든 82개를 채점자 둘이 매긴 라벨로 검증한 필터 (쓰레기 12개, 진짜 손실 0개).
    프롬프트로 금지하는 방법은 실패했다 — 진짜도 같이 줄어 커버리지가 69%→53%였다."""

    def g(self, source, phrase=""):
        return {"source_phrase": source, "phrase": phrase}

    def test_drops_what_a_still_cannot_show(self):
        for source in ("go very slowly as you don't want to", "너무 강하게 치지 않고",
                       "you can adjust how hard you want the torque",
                       "무릎을 번갈아 굽혀줍니다", "Beginners - Every other day"):
            self.assertTrue(analyze.unphotographable(self.g(source)), source)

    def test_drops_specs_already_precise_in_text(self):
        # 숫자가 단위에 붙어 있을 때만 잡는다 — "4 and a half millimeters"처럼
        # 숫자를 말로 푼 것은 놓친다 (알려진 한계, 오탐을 늘리지 않는 쪽을 택함)
        for source in ("50 grams", "they also come in 36 inches", "50% 이상이 파이썬"):
            self.assertTrue(analyze.unphotographable(self.g(source)), source)

    def test_drops_a_timestamp_masquerading_as_a_quote(self):
        self.assertTrue(analyze.unphotographable(self.g("9:17")))
        self.assertTrue(analyze.unphotographable(self.g(" 04:13 ")))

    def test_keeps_the_ambiguity_the_product_exists_for(self):
        for source, phrase in (("이 정도로", "이 정도로"), ("한입 크기로 썰어", "한입 크기"),
                               ("golden brown", "노릇노릇한 색"),
                               ("push it all the way in", "끝까지"),
                               ("so the holes are at the bottom", "구멍이 아래쪽으로")):
            self.assertEqual("", analyze.unphotographable(self.g(source, phrase)),
                             f"{source} / {phrase}")


class ReviewFindingsTests(unittest.TestCase):
    """외부 코드 리뷰(2026-08-08)가 잡은 결함들 — 전부 직접 재현한 뒤 고쳤다."""

    def test_short_phrase_does_not_swallow_a_distant_guide(self):
        # 'dough' 한 단어가 240초 떨어진 'knead the dough until smooth'를 삼켰다
        a = {"phrase": "dough", "what_to_show": "", "best_visual_timestamp": 10, "step_id": 1}
        b = {"phrase": "knead the dough until smooth and elastic", "what_to_show": "",
             "best_visual_timestamp": 250, "step_id": 1}
        self.assertFalse(analyze._same_guide(a, b))

    def test_step_without_timing_never_steals_a_guide(self):
        # t_start=None인 단계가 '0초에 있는 것'처럼 보여 최근접을 가로챘다
        steps = [{"id": 1, "t_start": 500, "t_end": 600},
                 {"id": 2, "t_start": None, "t_end": None},
                 {"id": 3, "t_start": 10, "t_end": 50}]
        self.assertEqual(3, analyze._step_for(5, steps))

    def test_renumber_closes_gaps_so_new_ids_cannot_collide(self):
        # trim 뒤 남은 id가 vg-1, vg-4, vg-5면 다음 새 id vg-4가 충돌한다
        guides = [{"id": f"vg-{i}", "importance": im}
                  for i, im in zip(range(1, 6), [0.9, 0.1, 0.2, 0.8, 0.7])]
        kept = analyze.renumber(analyze.trim_guides(guides, 3))
        self.assertEqual(["vg-1", "vg-2", "vg-3"], [g["id"] for g in kept])

    def test_reading_order_is_by_step_then_time(self):
        # importance 순으로 두면 한 단계 안에서 뒤 순간이 먼저 인쇄된다
        guides = [{"step_id": 1, "best_visual_timestamp": 280, "importance": 0.9},
                  {"step_id": 1, "best_visual_timestamp": 10, "importance": 0.3},
                  {"step_id": 2, "best_visual_timestamp": 5, "importance": 0.5}]
        ordered = analyze.order_for_reading(guides)
        self.assertEqual([(1, 10), (1, 280), (2, 5)],
                         [(g["step_id"], g["best_visual_timestamp"]) for g in ordered])

    def test_merge_runs_does_not_mutate_the_caller_data(self):
        steps = [{"id": 1, "t_start": 0, "t_end": 50}]
        first = {"steps": steps, "visual_guides": [
            {"id": "vg-1", "step_id": 1, "best_visual_timestamp": 10,
             "phrase": "a", "importance": 0.5}]}
        analyze.merge_runs([first, {"steps": steps, "visual_guides": []}], 0)
        self.assertNotIn("_analysis_passes", first)


class MergeRunsTests(unittest.TestCase):
    """같은 프롬프트를 두 번 돌리면 다른 가이드가 나온다 (실측: 기준선 재실행만으로 ±0.95개).
    프롬프트를 고쳐 더 뽑으려던 시도는 두 번 다 노이즈에 묻혔지만, 합집합은 2.8→5.2개였다."""

    def run_data(self, steps, guides):
        return {"steps": steps, "visual_guides": guides}

    def guide(self, gid, step_id, timestamp, phrase="p", importance=0.5):
        return {"id": gid, "step_id": step_id, "best_visual_timestamp": timestamp,
                "phrase": phrase, "importance": importance}

    def test_union_keeps_guides_from_every_run(self):
        steps = [{"id": 1, "t_start": 0, "t_end": 50}]
        merged = analyze.merge_runs([
            self.run_data(steps, [self.guide("vg-1", 1, 10, "마늘 다지기")]),
            self.run_data(steps, [self.guide("vg-1", 1, 40, "유화된 소스")]),
        ], max_guides=5)
        self.assertEqual(["마늘 다지기", "유화된 소스"],
                         [g["phrase"] for g in merged["visual_guides"]])
        self.assertEqual(["vg-1", "vg-2"], [g["id"] for g in merged["visual_guides"]])
        self.assertEqual(2, merged["_analysis_passes"])

    def test_same_moment_is_not_duplicated(self):
        steps = [{"id": 1, "t_start": 0, "t_end": 50}]
        merged = analyze.merge_runs([
            self.run_data(steps, [self.guide("vg-1", 1, 10, "마늘 다지기")]),
            self.run_data(steps, [self.guide("vg-1", 1, 11, "마늘을 다지는 크기")]),
        ], max_guides=5)
        self.assertEqual(1, len(merged["visual_guides"]))

    def test_guides_are_rehomed_by_timestamp_when_steps_differ(self):
        # 실행마다 단계 구조가 달라진다 (실측: 같은 영상이 4단계 → 6단계).
        # step_id를 그대로 쓰면 엉뚱한 단계에 붙으므로 시간으로 다시 잇는다.
        first = [{"id": 1, "t_start": 0, "t_end": 30}, {"id": 2, "t_start": 31, "t_end": 60}]
        second = [{"id": 1, "t_start": 0, "t_end": 60}]
        merged = analyze.merge_runs([
            self.run_data(first, [self.guide("vg-1", 1, 10)]),
            self.run_data(second, [self.guide("vg-1", 1, 45, "뒤쪽 순간")]),
        ], max_guides=5)
        rehomed = next(g for g in merged["visual_guides"] if g["phrase"] == "뒤쪽 순간")
        self.assertEqual(2, rehomed["step_id"])      # 45초는 첫 실행 기준 2단계

    def test_cap_is_respected_and_keeps_the_most_important(self):
        steps = [{"id": 1, "t_start": 0, "t_end": 90}]
        merged = analyze.merge_runs([
            self.run_data(steps, [self.guide("vg-1", 1, 10, "낮음", 0.2),
                                  self.guide("vg-2", 1, 30, "높음", 0.9)]),
            self.run_data(steps, [self.guide("vg-1", 1, 60, "중간", 0.5)]),
        ], max_guides=2)
        self.assertEqual(["높음", "중간"], [g["phrase"] for g in merged["visual_guides"]])

    def test_reworded_duplicate_is_merged(self):
        """실측: 합집합에서 'windowpane test showing translucent dough'와
        'translucent windowpane test'가 둘 다 남아 거의 같은 사진이 두 장 들어갔다."""
        steps = [{"id": 1, "t_start": 0, "t_end": 300}]
        merged = analyze.merge_runs([
            self.run_data(steps, [self.guide("vg-1", 1, 30,
                                             "windowpane test showing translucent dough")]),
            self.run_data(steps, [self.guide("vg-1", 1, 120, "translucent windowpane test")]),
        ], max_guides=5)
        self.assertEqual(1, len(merged["visual_guides"]))

    def test_different_moments_in_one_step_survive(self):
        # 같은 단계라도 내용이 다르면 남아야 한다 — 합치기가 과해지면 원래 문제로 돌아간다
        steps = [{"id": 1, "t_start": 0, "t_end": 300}]
        merged = analyze.merge_runs([
            self.run_data(steps, [self.guide("vg-1", 1, 30, "Guanciale cooked but not crispy")]),
            self.run_data(steps, [self.guide("vg-1", 1, 120, "Creamy emulsified pasta sauce")]),
        ], max_guides=5)
        self.assertEqual(2, len(merged["visual_guides"]))

    def test_guides_without_timestamp_are_dropped(self):
        steps = [{"id": 1, "t_start": 0, "t_end": 50}]
        merged = analyze.merge_runs([
            self.run_data(steps, [{"id": "vg-1", "step_id": 1,
                                   "best_visual_timestamp": None, "phrase": "장면 없음"}]),
            self.run_data(steps, [self.guide("vg-1", 1, 20, "쓸 만한 순간")]),
        ], max_guides=5)
        self.assertEqual(["쓸 만한 순간"], [g["phrase"] for g in merged["visual_guides"]])


class CaptureHeightTests(unittest.TestCase):
    """화면 녹화는 정보가 작은 UI 텍스트에 있어 480p에서 판독이 안 된다 (실측: 홀드아웃
    119건 중 '세 후보 모두 부적합' 5건의 절반이 software 도메인의 해상도 문제였다)."""

    def tearDown(self):
        os.environ.pop("STEPKEEPER_CAPTURE_HEIGHT", None)

    def test_default_stays_480(self):
        self.assertEqual(480, capture.capture_height({"category": "요리"}))
        self.assertEqual(480, capture.capture_height(None))
        self.assertEqual(480, capture.capture_height({}))

    def test_screen_content_escalates_in_every_output_language(self):
        # 카테고리는 모델이 출력 언어로 쓴다 — 한 언어만 보면 나머지가 조용히 새어나간다
        for category in ("소프트웨어", "Software", "ソフトウェア"):
            self.assertEqual(1080, capture.capture_height({"category": category}), category)

    def test_env_override_wins(self):
        os.environ["STEPKEEPER_CAPTURE_HEIGHT"] = "720"
        self.assertEqual(720, capture.capture_height({"category": "소프트웨어"}))
        self.assertEqual(720, capture.capture_height({"category": "요리"}))
        os.environ["STEPKEEPER_CAPTURE_HEIGHT"] = "이상한값"
        self.assertEqual(480, capture.capture_height({"category": "요리"}))


class CandidateMetaTests(unittest.TestCase):
    """재캡처 시 후보 타임스탬프가 달라지면 이전 선택은 거짓말이 된다 (외부 리뷰 P1-1):
    같은 vg-1_center.jpg 파일명이 전혀 다른 장면을 가리키는데 picks/근거가 남는다."""

    TIMES = {"vg-1": {"before": 7, "center": 8, "after": 9}}

    def test_first_capture_records_meta_without_invalidating(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            self.assertFalse(capture.sync_candidate_meta(out, self.TIMES))
            self.assertEqual(self.TIMES, json.loads(
                (out / "candidates.json").read_text(encoding="utf-8")))

    def test_same_times_keep_existing_picks(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            capture.sync_candidate_meta(out, self.TIMES)
            (out / "picks.json").write_text('{"vg-1": "center"}', encoding="utf-8")
            self.assertFalse(capture.sync_candidate_meta(out, dict(self.TIMES)))
            self.assertTrue((out / "picks.json").exists())

    def test_changed_times_invalidate_picks_and_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            capture.sync_candidate_meta(out, self.TIMES)
            (out / "picks.json").write_text('{"vg-1": "center"}', encoding="utf-8")
            (out / "picks-meta.json").write_text('{"reasons": {}}', encoding="utf-8")
            moved = {"vg-1": {"before": 6, "center": 8, "after": 10}}
            self.assertTrue(capture.sync_candidate_meta(out, moved))
            self.assertFalse((out / "picks.json").exists())
            self.assertFalse((out / "picks-meta.json").exists())
            self.assertEqual(moved, json.loads(
                (out / "candidates.json").read_text(encoding="utf-8")))

    def test_legacy_data_without_meta_fails_closed(self):
        """candidates.json 도입 전 데이터: 선택이 지금 후보와 맞는지 검증할 수 없다
        — 맞다고 가정하지 않는다 (창 규칙이 이미 ±4→±1/±2로 바뀐 뒤라 실제 위험)."""
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "picks.json").write_text('{"vg-1": "center"}', encoding="utf-8")
            self.assertTrue(capture.sync_candidate_meta(out, self.TIMES))
            self.assertFalse((out / "picks.json").exists())


class ExplicitSelectionTests(unittest.TestCase):
    def test_no_pick_never_auto_selects(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "vg-1_center.jpg").write_bytes(b"image")
            self.assertIsNone(renderer.choose_frame("vg-1", {}, path))

    def test_client_image_refs_override_disk_frames(self):
        data = CoreContractTests().valid_data()
        with tempfile.TemporaryDirectory() as temp:
            images_dir = Path(temp) / "images"
            images_dir.mkdir()
            ctx = renderer.build_context(
                "video", data, {}, Path(temp) / "no-frames", images_dir,
                image_refs={"vg-1": "https://cdn.example.com/vg-1.jpg"})
            guide = ctx["steps"][0]["visual_guides"][0]
            self.assertTrue(guide["has_screenshot"])
            self.assertEqual("https://cdn.example.com/vg-1.jpg", guide["screenshot"])

    def test_explicit_pick_selects_exact_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            candidate = path / "vg-1_before.jpg"
            candidate.write_bytes(b"image")
            self.assertEqual(candidate, renderer.choose_frame(
                "vg-1", {"vg-1": "before"}, path))

    def test_none_pick_forces_link_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(renderer.choose_frame(
                "vg-1", {"vg-1": "none"}, Path(temp)))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg 필요 (CI는 명시 설치)")
    def test_playable_rejects_header_only_video(self):
        """헤더만 있고 프레임이 없는 파일을 걸러낸다 (배치 검증 실측).

        yt-dlp가 exit 0으로 끝내고 48KB 조각을 남기는 경우가 있는데, ffprobe는
        "h264, 122초"로 정상 보고한다 — 한 장 디코드해야 판정된다. 이 파일이 캐시로
        남으면 이후 모든 실행이 알 수 없는 ffmpeg 에러로 죽는다.
        """
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.mp4"
            broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000)
            self.assertFalse(capture.playable(broken))
            missing = Path(temp) / "nope.mp4"
            self.assertFalse(capture.playable(missing))

    def test_hyphen_leading_video_id_survives_pipeline(self):
        """유튜브 ID는 '-'로 시작할 수 있다 (base64url) — argparse가 옵션으로 읽으면 안 된다.

        실측 실패: -kIaNu00a4s 로 파이프라인을 돌리면 capture 단계에서
        "the following arguments are required: video_id"로 죽었다.
        """
        from stepkeeper import pipeline
        cmd = pipeline.command("capture", "-kIaNu00a4s", "--profile", "generic")
        self.assertEqual("--", cmd[-2])
        self.assertEqual("-kIaNu00a4s", cmd[-1])
        # 실제로 argparse가 위치 인자로 받는지 (하위 파서와 같은 구성으로 재현)
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("video_id")
        parser.add_argument("--profile", default="generic")
        parsed = parser.parse_args(cmd[3:])   # [python, -m, module] 다음부터가 인자
        self.assertEqual("-kIaNu00a4s", parsed.video_id)

    def test_candidates_stay_near_center(self):
        """후보는 스텝 경계가 아니라 center 주변에서 뽑는다.

        회귀 방지(실측): 19초짜리 스텝에서 예전 규칙은 18·31·39초를 뽑았고, 18초는 이전
        섹션·39초는 다음 섹션이라 가이드가 요구한 26~29초 동작이 세 장 어디에도 없었다.
        """
        # 동작은 같은 동작 안에 머물도록 ±1초
        self.assertEqual(
            {"before": 6, "center": 7, "after": 8},
            capture.candidate_times({"t_start": 6, "t_end": 15},
                                    {"best_visual_timestamp": 7,
                                     "type": "action"}, 30))
        # 상태·위치 가이드도 상한 2초를 넘지 않는다
        self.assertEqual(
            {"before": 29, "center": 31, "after": 33},
            capture.candidate_times({"t_start": 19, "t_end": 38},
                                    {"best_visual_timestamp": 31,
                                     "type": "state"}, 82))
        # 아주 짧은 스텝에서도 최소 1초는 벌린다 (세 장이 같은 프레임이 되면 선택이 무의미)
        self.assertEqual(
            {"before": 9, "center": 10, "after": 11},
            capture.candidate_times({"t_start": 9, "t_end": 12},
                                    {"best_visual_timestamp": 10,
                                     "type": "state"}, 30))
        # 스텝 정보가 없어도 가이드 유형별 상한과 영상 경계를 지킨다
        self.assertEqual(
            {"before": 0, "center": 2, "after": 4},
            capture.candidate_times(None, {"best_visual_timestamp": 2,
                                           "type": "state"}, 30))
        self.assertEqual(
            {"before": 28, "center": 29, "after": 29},
            capture.candidate_times(None, {"best_visual_timestamp": 29,
                                           "type": "action"}, 30))


class ExportTests(unittest.TestCase):
    def test_bundle_and_obsidian_export(self):
        data = CoreContractTests().valid_data()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rendered = root / "rendered"
            (rendered / "images").mkdir(parents=True)
            (rendered / "images" / "vg-1_action.jpg").write_bytes(b"image")
            document = rendered / "document.md"
            document.write_text("![장면](images/vg-1_action.jpg)\n", encoding="utf-8")

            bundle = root / "bundle"
            exporter.export_bundle(data, rendered, document, bundle,
                                   "video", "generic", "ko")
            self.assertTrue((bundle / "manifest.json").exists())
            self.assertTrue((bundle / "images" / "vg-1_action.jpg").exists())

            vault = root / "vault"
            target = exporter.export_obsidian(data, rendered, document, vault,
                                              "video", "generic", "ko")
            text = target.read_text(encoding="utf-8")
            self.assertIn("attachments/테스트 가이드/vg-1_action.jpg", text)
            manifest = json.loads((vault / "테스트 가이드.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("ko", manifest["output_language"])

            (rendered / "images" / "vg-1_action.jpg").unlink()
            pdf = exporter.export_goodnotes(
                data, rendered, root / "goodnotes", "video")
            self.assertTrue(pdf.exists())
            self.assertGreater(pdf.stat().st_size, 1000)


class AutoPickTests(unittest.TestCase):
    def _seed(self, root: Path):
        os.environ["STEPKEEPER_DATA"] = str(root)
        data = CoreContractTests().valid_data()
        from stepkeeper.common import analysis_file, frames_dir
        source = analysis_file(root, "vid00000000", "generic", "ko")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        frames = frames_dir(root, "vid00000000", "generic", "ko")
        frames.mkdir(parents=True, exist_ok=True)
        for slot in ("before", "center", "after"):
            (frames / f"vg-1_{slot}.jpg").write_bytes(b"\xff\xd8fake")
        return frames

    def tearDown(self):
        os.environ.pop("STEPKEEPER_DATA", None)

    @staticmethod
    def _with_verify(pick_response, shows=True, verify_reason=""):
        """선택 응답 뒤의 자기 검증 호출까지 흉내 내는 side_effect."""
        def fake(parts, model, key, schema):
            if "shows" in schema.get("properties", {}):
                return {"shows": shows, "reason": verify_reason}
            return pick_response
        return fake

    def test_auto_pick_writes_picks_and_meta(self):
        from stepkeeper import autopick
        with tempfile.TemporaryDirectory() as temp:
            frames = self._seed(Path(temp))
            with patch.object(autopick, "generate_json", side_effect=self._with_verify({
                    "picks": [{"guide_id": "vg-1", "slot": "after",
                               "reason": "목표 상태가 가장 명확"}]})):
                picks = autopick.auto_pick("vid00000000", "generic", "ko", "m", "k")
            self.assertEqual({"vg-1": "after"}, picks)
            saved = json.loads((frames / "picks.json").read_text(encoding="utf-8"))
            self.assertEqual("after", saved["vg-1"])
            meta = json.loads((frames / "picks-meta.json").read_text(encoding="utf-8"))
            self.assertEqual("auto", meta["source"])

    def test_missing_guides_fall_back_to_none(self):
        from stepkeeper import autopick
        with tempfile.TemporaryDirectory() as temp:
            self._seed(Path(temp))
            with patch.object(autopick, "generate_json",
                              return_value={"picks": []}) as mock:
                picks = autopick.auto_pick("vid00000000", "generic", "ko", "m", "k")
            self.assertEqual({"vg-1": "none"}, picks)
            self.assertEqual(1, mock.call_count)   # none에는 검증 호출도 없다

    def test_verification_rejects_pick_and_falls_back_to_none(self):
        """자기 검증 패스: 고른 한 장을 다시 보여 '정말 보이는가'를 묻는다.

        실측 #6 — 렌치가 어느 후보에도 없는데 '그중 제일 나은' center를 골랐다.
        후보 비교 편향을 끊으려면 선택된 프레임 단독으로 재검증해야 한다.
        """
        from stepkeeper import autopick
        with tempfile.TemporaryDirectory() as temp:
            frames = self._seed(Path(temp))
            with patch.object(autopick, "generate_json", side_effect=self._with_verify(
                    {"picks": [{"guide_id": "vg-1", "slot": "center", "reason": "제일 낫다"}]},
                    shows=False, verify_reason="렌치가 보이지 않음")):
                picks = autopick.auto_pick("vid00000000", "generic", "ko", "m", "k")
            self.assertEqual({"vg-1": "none"}, picks)
            meta = json.loads((frames / "picks-meta.json").read_text(encoding="utf-8"))
            self.assertEqual("렌치가 보이지 않음", meta["reasons"]["vg-1"])

    def test_each_guide_gets_an_isolated_request(self):
        """가이드별 독립 호출 (외부 리뷰 P2-4): 묶음 호출에서는 앞 가이드의 장면이
        뒤 가이드의 근거에 새어 들어왔다 (실측 ㄱ20). 요청에 다른 가이드가 섞이지
        않아야 하고, 응답이 다른 가이드를 언급해도 받아들이지 않아야 한다."""
        from stepkeeper import autopick
        from stepkeeper.common import analysis_file
        with tempfile.TemporaryDirectory() as temp:
            frames = self._seed(Path(temp))
            source = analysis_file(Path(temp), "vid00000000", "generic", "ko")
            data = json.loads(source.read_text(encoding="utf-8"))
            second = dict(data["visual_guides"][0], id="vg-2",
                          phrase="두 번째 가이드")
            data["visual_guides"].append(second)
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            for slot in ("before", "center", "after"):
                (frames / f"vg-2_{slot}.jpg").write_bytes(b"\xff\xd8fake2")

            calls = []

            def fake_generate(parts, model, key, schema):
                text = "\n".join(part.get("text", "") for part in parts)
                if "shows" in schema.get("properties", {}):
                    return {"shows": True}
                calls.append(text)
                asked = "vg-1" if "[vg-1]" in text else "vg-2"
                return {"picks": [
                    {"guide_id": asked, "slot": "center", "reason": "ok"},
                    # 다른 가이드를 참칭하는 항목은 무시되어야 한다
                    {"guide_id": "vg-1" if asked == "vg-2" else "vg-2",
                     "slot": "after", "reason": "누수"},
                ]}

            with patch.object(autopick, "generate_json", side_effect=fake_generate):
                picks = autopick.auto_pick("vid00000000", "generic", "ko", "m", "k")
            self.assertEqual(2, len(calls))
            self.assertNotIn("vg-2", calls[0])   # 요청 자체가 격리
            self.assertNotIn("[vg-1]", calls[1])
            self.assertEqual({"vg-1": "center", "vg-2": "center"}, picks)


class FeedbackTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("STEPKEEPER_DATA", None)

    def test_add_and_summary(self):
        from stepkeeper import feedback
        with tempfile.TemporaryDirectory() as temp:
            os.environ["STEPKEEPER_DATA"] = temp
            evaluation = Path(temp) / "semantic-evaluation.json"
            evaluation.write_text(json.dumps({
                "video_id": "v", "profile": "generic", "language": "ko",
                "guides": [
                    {"guide_id": "vg-1", "ai_slot": "center",
                     "selected_slot": "center", "reviewed": True},
                    {"guide_id": "vg-2", "ai_slot": "center",
                     "selected_slot": "after", "reviewed": True},
                    {"guide_id": "vg-3", "ai_slot": None, "reviewed": True},
                ]}), encoding="utf-8")
            self.assertEqual(2, feedback.add(evaluation))
            stats = feedback.summary()
            self.assertEqual(2, stats["total"])
            group = stats["evaluations"]["(pre-batch)"]
            self.assertEqual(1, group["agreed"])
            self.assertEqual({"center→after": 1}, group["disagreements"])

    def test_add_accepts_batch_evaluation_and_dedups(self):
        """배치 평가(records/human_slot) 스키마도 공식 경로로 읽힌다 (외부 리뷰 P1-2).

        실측: recovery-review.json을 add에 넣었더니 '기록됨: 0건'이었다 — 기준선
        데이터가 공식 경로 밖에 있으면 다음 회귀 비교가 손으로 하는 일이 된다.
        같은 평가를 두 번 add해도 중복 기록되지 않아야 한다.
        """
        from stepkeeper import feedback
        with tempfile.TemporaryDirectory() as temp:
            os.environ["STEPKEEPER_DATA"] = temp
            evaluation = Path(temp) / "batch-review.json"
            evaluation.write_text(json.dumps({
                "evaluation_id": "2026-08-02-test",
                "review_kind": "recovery_rerun",
                "records": [
                    {"video_id": "v1", "profile": "generic", "language": "en",
                     "guide_id": "vg-1", "ai_slot": "center",
                     "human_slot": "center", "candidate_hit": True},
                    {"video_id": "v1", "profile": "generic", "language": "en",
                     "guide_id": "vg-2", "ai_slot": "none",
                     "human_slot": "before", "candidate_hit": True},
                ]}), encoding="utf-8")
            self.assertEqual(2, feedback.add(evaluation))
            self.assertEqual(0, feedback.add(evaluation))     # 재실행 = 멱등
            stats = feedback.summary()
            group = stats["evaluations"]["2026-08-02-test"]
            self.assertEqual(2, group["total"])
            self.assertEqual(1, group["agreed"])
            self.assertEqual(1.0, group["candidate_coverage"])

    def test_summary_keeps_evaluations_apart(self):
        """회차가 다른 평가는 합산하지 않는다 — 섞인 누적치는 기준선이 아니다."""
        from stepkeeper import feedback
        with tempfile.TemporaryDirectory() as temp:
            os.environ["STEPKEEPER_DATA"] = temp
            for eval_id, slot in (("round-1", "after"), ("round-2", "center")):
                path = Path(temp) / f"{eval_id}.json"
                path.write_text(json.dumps({
                    "evaluation_id": eval_id,
                    "records": [{"video_id": "v", "profile": "generic",
                                 "language": "ko", "guide_id": "vg-1",
                                 "ai_slot": "center", "human_slot": slot}],
                }), encoding="utf-8")
                feedback.add(path)
            stats = feedback.summary()
            self.assertEqual(2, stats["total"])
            self.assertEqual(0, stats["evaluations"]["round-1"]["agreed"])
            self.assertEqual(1, stats["evaluations"]["round-2"]["agreed"])


class NotionTests(unittest.TestCase):
    def test_block_building_with_image_and_link(self):
        data = CoreContractTests().valid_data()
        data["visual_guides"].append({
            "id": "vg-2", "step_id": 1, "source_phrase": "x", "phrase": "링크 가이드",
            "type": "state", "what_to_show": "y", "best_visual_timestamp": 9,
            "guide_text": "링크로 확인한다.", "importance": 0.5})
        blocks = exporter.build_notion_blocks(data, "vid00000000", {"vg-1": "upload-1"})
        kinds = [block["type"] for block in blocks]
        self.assertIn("image", kinds)
        image = next(block for block in blocks if block["type"] == "image")
        self.assertEqual("upload-1", image["image"]["file_upload"]["id"])
        links = [block for block in blocks if block["type"] == "paragraph"
                 and block["paragraph"]["rich_text"][0]["text"].get("link")]
        self.assertTrue(any("t=9" in block["paragraph"]["rich_text"][0]["text"]["link"]["url"]
                            for block in links))

    def test_notion_blocks_follow_output_language(self):
        """Notion 절 제목도 --language를 따라야 한다 (문서 뼈대와 같은 규칙).

        회귀 방지: 예전에는 '준비물'·'순서'·'기준:'·'YouTube 원본'이 하드코딩이라
        영어 문서를 Notion으로 보내면 한국어 제목이 붙었다.
        """
        def headings(language, profile="generic"):
            data = CoreContractTests().valid_data()
            data["_output_language"] = language
            data["_profile"] = profile
            blocks = exporter.build_notion_blocks(data, "vid00000000", {})
            return [b["heading_2"]["rich_text"][0]["text"]["content"]
                    for b in blocks if b["type"] == "heading_2"]

        self.assertIn("Steps", headings("en"))
        self.assertIn("What you need", headings("en"))
        self.assertIn("Ingredients", headings("en", profile="recipe"))
        self.assertIn("순서", headings("ko"))
        self.assertIn("준비물", headings("ko"))
        self.assertIn("준비 재료", headings("ko", profile="recipe"))
        self.assertIn("手順", headings("ja"))
        self.assertIn("Steps", headings("de"))          # 번역본 없는 언어는 영어

        data = CoreContractTests().valid_data()
        data["_output_language"] = "en"
        blocks = exporter.build_notion_blocks(data, "vid00000000", {})
        text = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("Watch on YouTube", text)
        self.assertIn("What '", text)                   # 가이드 접두사
        self.assertNotIn("기준:", text)

    def test_export_notion_uploads_and_creates_page(self):
        data = CoreContractTests().valid_data()
        calls = []

        def fake_request(path, token, payload=None, data=None, content_type=None):
            calls.append(path)
            if path == "/file_uploads":
                return {"id": "up-1"}
            if path.startswith("/file_uploads/"):
                return {"status": "uploaded"}
            if path == "/pages":
                self.assertEqual("parent-page", payload["parent"]["page_id"])
                return {"id": "page-1", "url": "https://notion.so/page-1"}
            if path.startswith("/blocks/"):
                return {"results": []}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as temp:
            rendered = Path(temp)
            (rendered / "images").mkdir()
            (rendered / "images" / "vg-1_action.jpg").write_bytes(b"img")
            with patch.object(exporter, "notion_request", side_effect=fake_request):
                url = exporter.export_notion(data, rendered, "vid00000000",
                                             "parent-page", "tok")
        self.assertEqual("https://notion.so/page-1", url)
        self.assertIn("/file_uploads", calls)
        self.assertIn("/pages", calls)
        # 페이지가 업로드보다 **먼저** — 가장 흔한 실패(부모 페이지 문제)가 업로드 0건으로 끝난다.
        # Notion API에는 업로드 삭제가 없어 순서가 유일한 방어다.
        self.assertLess(calls.index("/pages"), calls.index("/file_uploads"))

    def test_export_notion_uploads_nothing_when_page_creation_fails(self):
        """부모 페이지 오류로 페이지를 못 만들면 이미지는 한 장도 올라가지 않아야 한다."""
        data = CoreContractTests().valid_data()
        calls = []

        def fake_request(path, token, payload=None, data=None, content_type=None):
            calls.append(path)
            if path == "/pages":
                raise RuntimeError("404 parent not found")
            raise AssertionError(f"페이지 생성 실패 후 호출됨: {path}")

        with tempfile.TemporaryDirectory() as temp:
            rendered = Path(temp)
            (rendered / "images").mkdir()
            (rendered / "images" / "vg-1_action.jpg").write_bytes(b"img")
            with patch.object(exporter, "notion_request", side_effect=fake_request):
                with self.assertRaises(RuntimeError):
                    exporter.export_notion(data, rendered, "vid00000000", "bad-parent", "tok")
        self.assertEqual(["/pages"], calls)


if __name__ == "__main__":
    unittest.main()


class JunkFilterTests(unittest.TestCase):
    """되묻기가 만드는 쓰레기를 코드로 거른다. 프롬프트로 금지하려던 시도는 실패했지만
    (좋은 가이드가 47→30개로 같이 사라짐) 출력 검사는 라벨 82건에서 정밀도 100%였다."""

    def junk(self, phrase, source=""):
        return analyze.junk_reason({"phrase": phrase, "source_phrase": source})

    def test_timestamp_as_source_is_rejected(self):
        # 조인 프롬프트가 만든 실측 결함 — 추가분의 22%가 원문에 타임코드를 넣었다
        self.assertIsNotNone(self.junk("top panel holes", "04:13"))
        self.assertIsNotNone(self.junk("어떤 것", "9:17"))

    def test_numeric_specs_are_rejected(self):
        for phrase in ("50 grams", "4.5mm", "36 inches", "2 inches in width"):
            self.assertIsNotNone(self.junk(phrase), phrase)

    def test_frequency_and_effort_are_rejected(self):
        # 규칙 10: 한 장의 정지 화면으로 확인할 수 없는 조언
        for phrase in ("Every other day", "두 번 연속으로", "아주 천천히", "너무 강하게 치지 않고"):
            self.assertIsNotNone(self.junk(phrase), phrase)

    def test_real_guides_survive(self):
        # 라벨 82건에서 오탐 0을 확인한 문구들 — 여기가 깨지면 진짜 가이드를 잃는다
        for phrase in ("잘게 다진 마늘", "힙 너비", "어깨 높이", "windowpane test showing translucent dough",
                       "소스와 면이 엉긴 상태", "무릎 바깥쪽", "금속 플러그 뾰족한 부분의 방향"):
            self.assertIsNone(self.junk(phrase), phrase)

    def test_drop_junk_reports_what_it_removed(self):
        kept, dropped = analyze.drop_junk([
            {"phrase": "잘게 다진 마늘"}, {"phrase": "50 grams"}, {"phrase": "아주 천천히"}])
        self.assertEqual(["잘게 다진 마늘"], [g["phrase"] for g in kept])
        self.assertEqual(2, len(dropped))
        self.assertIn("50 grams", dropped[0])
