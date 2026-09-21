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

# offline judge: a local Ollama model, nothing leaves the machine
python3 council.py judge --judge ollama --brief BRIEF.md --proposals a.md b.md c.md

# change weights or thresholds later without asking the judge again
python3 council.py rescore --report run.report.json --rubric my_rubric.json
```

Exit code 0 means *recommend*, 1 means *escalate*, 2 means the judge could not start.

## How a decision is made

1. Proposals are shuffled and relabelled `Proposal A, B, ...`. The judge sees only the brief and the proposal text. The label-to-file map stays in the report.
2. Each proposal is judged **independently** with the same questions over the same brief, one request per proposal, so scores are comparable. Because no proposal is judged next to another, presentation order cannot bias the judge.
3. Each `Score` is normalised to 0..1 and combined with the rubric weights. A `Noul` veto at or above its threshold removes a proposal regardless of its score.
4. The leader is recommended only if it beats the runner-up by `min_margin`, the judge's lowest confidence on it is at least `min_confidence`, and every proposal was judged. Otherwise the council escalates and lists the reasons.

`rubrics/system_design.json` scores constraint fit, failure handling, verifiability, simplicity, reversibility and evidence, and vetoes proposals that depend on something the brief rules out or that answer a different question. Its weights and thresholds are starting points, not findings.

## What is and is not validated

- 21 offline tests: request shape against the documented API contract, retry on 429/529, no retry on 401/422, the API key never appearing in errors, vetoes, margin and confidence gates, judge failures, and `rescore` making no judge call.
- The worked example was run end to end with the **offline Ollama judge** (`qwen2.5:7b`). It vetoed the proposal that depends on SSH, which the brief forbids, and recommended the measured-decision proposal, identically under two shuffles.
- **The TypeSafe judge has not been run against the live API.** No key was available. Its request and error handling are tested against the documented contract only.
- That example proves plumbing, not taste: one author wrote the rubric and all three proposals. The same run also vetoed the SSH proposal for "answering a different question", which is a false positive.
- Before trusting it, replay real past decisions whose outcome you know and compare. A council tends toward the consensus answer; whether that matches your judgement is an empirical question.

## Limits

- Text only. It can judge written designs, not diagrams or mockups.
- One judge is one opinion, however many times it is asked. Diversity has to come from the proposals and the rubric. A second judge seat is a natural next step.
- A proposal can reveal its author in its own text. Anonymisation covers file names only.
- With the TypeSafe judge, the brief and every proposal are sent to a third-party API. Use `show-request` first, and do not put anything in a brief that you would not send.
