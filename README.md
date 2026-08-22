# stepkeeper

Turns videos into documents, recipes, and user manuals.

Instructions like *"cut it bite-sized"* or *"simmer until the sauce reduces"* don't mean much as text. stepkeeper finds the frame where that state is actually visible and embeds it next to the step. It works across domains — cooking, repair, crafts, beauty, fitness, software — and exports to Notion, Obsidian, and Goodnotes.

Gemini analyzes the video itself (visuals and audio), so it works on videos without captions, and when the narration runs ahead of the action.

This repo is the Python core and the language-neutral `skill-core/` assets. If you just want to use
stepkeeper, the clients built on it are [stepkeeper-apple](https://github.com/zlej123/stepkeeper-apple)
(iOS/iPadOS/macOS) and [stepkeeper-extension](https://github.com/zlej123/stepkeeper-extension) (Chrome).

![stepkeeper demo — a spoken "bite-sized" becomes the frame that shows it](docs/demo/demo.gif)

## Example

Generated from [this pork stir-fry video](https://youtu.be/4ioPBiTWm3M). Where the video only says *"simmer until the sauce reduces"*, the document reads:

> 2\. **Simmer the pork in the sauce**
> - Add 1/2 cup water, 1T brown sugar, 1T syrup to the pan; once dissolved, add the pork. …
> - 💡 *"Reduced" means:* almost no liquid left on the pan bottom, sauce clinging to the meat with a glossy sheen.
>
> ![reduced sauce state](docs/demo/demo-state.jpg)

And *"cut it bite-sized"*:

> - 💡 *"Bite-sized" means:* roughly 3–4 cm cubes.
>
> ![bite-sized pork](docs/demo/demo-size.jpg)

## Install

```bash
pip install -e .                   # installs deps + the `stepkeeper` command
# system dependency: ffmpeg (on PATH; not needed for --links-only)
export GEMINI_API_KEY=...          # Google AI Studio key
```

## Usage

One command runs the whole pipeline.

```bash
# 1) Fully automatic, links instead of screenshots (no ffmpeg)
stepkeeper "https://www.youtube.com/watch?v=..." --profile generic --language en --links-only

# 2) Fully automatic with screenshots: AI picks the frames, you review after
stepkeeper "https://www.youtube.com/watch?v=..." --profile recipe --language en --auto-pick --export goodnotes

# 3) Manual frame selection
stepkeeper "https://www.youtube.com/watch?v=..." --profile recipe --language en
#   → open the printed picker.html, pick one candidate per guide, save picks.json
stepkeeper "https://www.youtube.com/watch?v=..." --profile recipe --language en \
    --picks work/frames/<id>/recipe.en/picks.json --export goodnotes
```

Options: `--profile generic|recipe`, `--language ko|en|ja|...`, `--max-guides N`, `--model`, `--auto-pick`, `--export bundle|obsidian|goodnotes|notion` (Notion also needs `--parent <page-id>` and `NOTION_TOKEN`).

With `--auto-pick`, Gemini vision chooses among the three candidates per guide (or falls back to a
timestamp link when none fits). The regenerated picker.html shows the AI picks pre-selected; if you
correct any, download the evaluation file and record it:

```bash
python -m stepkeeper.feedback add semantic-evaluation.json   # accumulates accuracy + disagreement patterns
```

Artifacts are written under the current directory (override with `STEPKEEPER_DATA`).

`--passes 2` analyzes the video twice and merges the guides. The model is non-deterministic enough
that two runs surface *different* ambiguous moments, so the union is denser than either run — 2.8 →
4.0 guides per video, measured over ten videos. Near-duplicates are merged. Use it when a long video
comes back with a thin document; it costs one extra analysis call per pass.

Candidates are taken from a fixed window around the analysis timestamp (center ±1–2 s, clamped to
the step). `--search` instead scans the step's whole time range — frames extracted locally, one
cheap vision call — and anchors the three candidates where the target is actually visible, keeping
the analysis timestamp as one of the three. If the scan finds nothing, returns a time outside the
step, or fails, it falls back to the fixed window.

**`--search` is opt-in because it is not established as an improvement.** A 95-guide A/B measured
net +2 guides (p = 0.75 — smaller than the average swing a pure-noise rerun produces), and the run
was confounded: the baseline arm ran on a different model string. An earlier claim here that
"search can only match or beat the baseline" was false — search alone lost photos on 5 guides while
gaining on 4. The cost, unlike the benefit, is certain: roughly 19x the selection-stage tokens. See
`feedback/evaluations/2026-08-13-adaptive-search-ab-inconclusive.json` for the experiment that would
settle it.

Frames are captured from a 480p download. Screen recordings are pulled at 1080p instead — their
information lives in small UI text that 480p destroys, and static screen content compresses well
enough that the bigger file costs little. Override either with `STEPKEEPER_CAPTURE_HEIGHT`.

## Note app export

| Target | How | Status |
|--------|-----|--------|
| Obsidian | Markdown + attachments copied into a vault folder | done |
| Goodnotes | PDF for the import/share flow. CJK works when a system CJK font registers (probed automatically; `--font` to override) — a loud warning is printed otherwise | done |
| Notion | direct upload via the Notion API (your integration token) | done |

```bash
stepkeeper-export <id> --profile recipe --language en --target obsidian --destination /path/to/vault
stepkeeper-export <id> --profile recipe --language en --target goodnotes
NOTION_TOKEN=... stepkeeper-export <id> --profile recipe --language en --target notion --parent <page-id>
```

## Reusing stepkeeper

Two reuse boundaries:

1. **`skill-core/`** — language-neutral assets: `profiles/<name>/{prompt.md, schema.json, template.md}` and `engine/rules.md`. Any platform can consume these as data.
2. **The Python modules** — reusable wherever Python runs.

| Consumer | How |
|----------|-----|
| REST API server | wraps the modules — see [stepkeeper-server](https://github.com/zlej123/stepkeeper-server) |
| Desktop app / Python tools / agent skills | import directly (see `skills/stepkeeper/SKILL.md`) |
| iOS/iPadOS/macOS app | [stepkeeper-apple](https://github.com/zlej123/stepkeeper-apple) — bundles `skill-core/` and calls Gemini directly (no server), with the Python renderer ported to Swift |
| Browser | [stepkeeper-extension](https://github.com/zlej123/stepkeeper-extension) — captures frames from the YouTube player itself · [Chrome Web Store](https://chromewebstore.google.com/detail/ckgcfpdlfihclbgmigfnniepkeilcgbp) |

Both clients capture frames on their own side (WKWebView / canvas), so neither needs ffmpeg or a
download step, and the server stays optional. stepkeeper-apple is the fullest reuse of `skill-core/`:
its Swift port of the mustache renderer is pinned to this repo's `render.py` output by golden tests,
so a template change here stays reproducible there.

## Use as an agent skill

stepkeeper ships as an agent skill (`skills/stepkeeper/SKILL.md`).

- **Claude Code**: `/plugin marketplace add zlej123/stepkeeper`, then `/plugin install stepkeeper@stepkeeper`.
- **Manual**: copy `skills/stepkeeper/` into your skills directory (`~/.claude/skills/` or `~/.gjc/skills/`).

The skill clones this repo on first use and asks for a Gemini API key if none is set.

What the skill does once installed: you paste a how-to URL, it runs analyze → capture → pick →
render, and hands back a document. The agent can pick the frames itself by reading the three
candidates per guide, or hand you `picker.html` to choose. Ambiguous instructions are the whole
point — see the demo above.

The demo GIF is assembled from real pipeline output by `docs/demo/make_demo_gif.py`
(headless Chrome + ffmpeg); regenerate it after changing the frames or the wording.

## Adding a domain profile

Drop three files into `src/stepkeeper/skill-core/profiles/<name>/`: `prompt.md` (containing `{{RULES}}`), `schema.json`, `template.md`. No pipeline changes needed.

## Tests

```bash
python -m unittest discover -s tests        # contract / normalization / selection / export
python tests/validate_fixtures.py --online  # fixture availability + strata
python tests/batch.py                        # domain structural + semantic regression
python tests/batch.py --domain en_output --analyze   # English document-language smoke
```

`tests/fixtures/urls.json` is a regression corpus of 8–12 videos per domain, stratified by length, audio, captions, editing style, framing, and source language. The `en_output` suite (`suite: smoke`) reuses a few English-source videos with `--language en` so English **output** stays regression-covered for GitHub EN users (separate from Korean-output domain runs). After capture, drop picker evaluations at `tests/evaluations/<id>.<profile>.en.json`.

## Limits

- Public videos only; under 30 minutes recommended.
- Free-tier Gemini rate-limits under batch load. Default model is `gemini-3.5-flash-lite`.
- Timestamps are accurate to about ±2–3 s; the before/center/after candidates cover the gap.
- Not useful for videos with nothing visual to show (lectures, vlogs, reviews).

## License

MIT
