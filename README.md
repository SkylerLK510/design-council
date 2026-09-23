# Design Council

A personal experiment: use a council to advise on system and API design decisions.

Modelled on Karpathy's [llm-council](https://github.com/karpathy/llm-council) (independent answers, anonymised peer review, chairman synthesis) with one change of shape. Proposals come from anywhere as text files. A judge scores each one against an explicit rubric with typed questions. **Code is the chairman**: weights, vetoes and a confidence gate decide between *recommend* and *escalate to a human*, and the report says why. It advises. Nothing acts on its output.

The default judge is [TypeSafe's System One API](https://docs.typesafe.ai/api): `Score` questions per rubric dimension, `Noul` questions for vetoes, with probabilities and confidence. Standard library only, Python 3.11+.

## Use

```sh
# see exactly what would be sent, without sending anything
python3 council.py show-request --brief examples/coordinator-host/brief.md --proposals examples/coordinator-host/*.md

# judge with TypeSafe (needs TYPESAFE_API_KEY in the environment)
python3 council.py judge --brief BRIEF.md --proposals a.md b.md c.md --out run.report.json

# pin a model version instead of the moving jev-latest alias
python3 council.py judge --typesafe-model jev-1.13.0 --brief BRIEF.md --proposals a.md b.md c.md

# offline judge: a local Ollama model, nothing leaves the machine
python3 council.py judge --judge ollama --brief BRIEF.md --proposals a.md b.md c.md

# change weights or thresholds later without asking the judge again
python3 council.py rescore --report run.report.json --rubric my_rubric.json

# try rescore with no key, on a saved live report of the worked example
python3 council.py rescore --report examples/coordinator-host/live-run.jev-1.13.0.json --rubric rubrics/system_design.json
```

A glob over a folder may include the brief itself. It is left out of the proposals, with a note on stderr.

Exit code 0 means *recommend*, 1 means *escalate* (for `judge` and `rescore` alike), 2 means the judge could not start.

## How a decision is made

1. Proposals are shuffled and relabelled `Proposal A, B, ...`. The judge sees only the brief and the proposal text. The label-to-file map stays in the report.
2. Each proposal is judged **independently** with the same questions over the same brief, one request per proposal, so scores are comparable. Because no proposal is judged next to another, presentation order cannot bias the judge.
3. Each `Score` is normalised to 0..1 and combined with the rubric weights. A `Noul` veto at or above its threshold removes a proposal regardless of its score.
4. The leader is recommended only if it beats the runner-up by `min_margin`, the judge's lowest confidence on it is at least `min_confidence`, and every proposal was judged. Otherwise the council escalates and lists the reasons.

The report header names the model version that answered and the input tokens used. `jev-latest` is an alias that moves when TypeSafe ships a release, so once `min_confidence` and the veto thresholds have been tuned against real decisions, pin that version with `--typesafe-model` and move on your own schedule.

`rubrics/system_design.json` scores constraint fit, failure handling, verifiability, simplicity, reversibility and evidence, and vetoes proposals that depend on something the brief rules out or that leave the decision unanswered (a rule that says which option to pick once named facts are checked counts as an answer). Its weights and thresholds are starting points, not findings. `rescore` only accepts a rubric that asks exactly the same questions: reworded instructions or levels need a new judge run.

## What is and is not validated

Offline:

- 26 tests: request shape against the documented API contract, retry on 429/529 (honouring a numeric `Retry-After`), no retry on 401/422, the API key never appearing in errors, model pinning, a proposals glob that also matches the brief, vetoes, margin and confidence gates, judge failures, `rescore` making no judge call and refusing reworded questions, and `rescore` of the saved live run exiting like `judge`.

Live, against `jev-1.13.0` on 2026-09-23:

- **The worked example** (3 requests, about 4.5k input tokens, 0.2 to 0.7 s per request). `wsl2.md` is vetoed at 0.95 to 0.96 for depending on SSH, which the brief forbids. `measure-then-decide.md` leads `native-windows.md` by 0.12 to 0.13 in every run. The report is saved as `examples/coordinator-host/live-run.jev-1.13.0.json`.
- **Repeat runs are close but not identical.** Across identical requests, totals moved by up to 0.018, a Noul by up to 0.05 and a Score's confidence by up to 0.17. Keep `min_margin` and veto thresholds well clear of that.
- **Three rubric questions were reworded after live A/B tests.** Each variant was asked next to the original in the same request.
  - `simplicity` mixed two things: adding parts the goal does not need, and naming the moving parts. On the measured-decision proposal Jev split between them with confidence 0.00. It now asks only what a proposal adds, and says that a step checking something the brief calls unknown counts as needed (confidence 0.43 to 0.46). Removing the second half without that sentence left confidence at 0.00.
  - The old veto "answers a different question" gave the measured-decision proposal 0.62 to 0.65, just under its 0.7 threshold, and gave a proposal that recommends nothing only 0.55. Its replacement, `leaves_decision_unanswered`, gives 0.21 and 0.92 to 0.95 on the two decoys in `examples/probes/`, which answer a different decision and make no recommendation.
  - `constraint_fit` now says that a constraint the proposal never mentions counts as unaddressed (see the blind review below).
- **Blind review.** For each real proposal, two graders who never saw Jev's answers placed it on every question, and an adjudicator compared them with Jev. The graders and adjudicator are Claude subagents, so they are a second opinion, not ground truth. Jev agreed with them on all six veto answers. Of the 18 score placements, 11 were defensible, 3 were rubric ambiguity and 4 were Jev over-crediting a level that requires several conditions at once, such as "addresses every constraint" or "cites or measures the facts". Rewording `constraint_fit` fixed one of its two misplacements and left the other at confidence 0.09 to 0.14, which the confidence gate catches. Scored on the graders' levels, the ranking is the same: 0.83 against 0.60, with the SSH proposal vetoed.
- **The example still escalates.** The leader's lowest confidence (0.21 to 0.38, on `constraint_fit`) is below `min_confidence`, even with a 0.12 lead. Whether the gate should use the lowest confidence on any question or the chance that the ranking flips is an open design question.
- The offline Ollama judge (`qwen2.5:7b`) was only run on the earlier rubric. It vetoed the SSH proposal correctly, and also vetoed it for "answering a different question", which was a false positive.

What that does not show:

- Taste. One author wrote the rubric and all the proposals, and the rewordings were tested on three proposals and two decoys, so they may fit this example better than the next one.
- Before trusting it, replay real past decisions whose outcome you know and compare. A council tends toward the consensus answer; whether that matches your judgement is an empirical question.

## Limits

- Text only. It can judge written designs, not diagrams or mockups.
- One judge is one opinion, however many times it is asked. Diversity has to come from the proposals and the rubric. A second judge seat is a natural next step.
- A proposal can reveal its author in its own text. Anonymisation covers file names only.
- A proposal can argue for itself ("this design meets every constraint"). TypeSafe lists adversarial or self-describing content among Jev 1.13's known weak spots, so the rubric scores what a proposal shows, not what it claims, and a persuasive proposal still deserves a human read.
- With the TypeSafe judge, the brief and every proposal are sent to a third-party API. Use `show-request` first, and do not put anything in a brief that you would not send.
