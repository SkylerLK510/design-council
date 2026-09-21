"""Design council: several proposals in, one ADVISORY recommendation out.

After Karpathy's llm-council, with one change of shape. There, LLMs answer, LLMs
rank each other in prose, and a chairman LLM writes the verdict. Here:

  1. proposals come from anywhere (you, Claude, Codex, a local model) as text files
  2. they are anonymised and shuffled, then a judge scores each one against an
     explicit rubric: typed Score questions per dimension, Noul questions for vetoes
  3. CODE is the chairman: weights, veto thresholds and a confidence gate decide
     between "recommend X" and "escalate to a human", and say why

The default judge is TypeSafe's System One API (typed answers with probabilities and
confidence, https://docs.typesafe.ai/api). Raw answers are kept in the report, so
weights can change later without asking the judge again.

Standard library only. The API key is read from TYPESAFE_API_KEY and is never
printed or written. `show-request` prints exactly what would leave the machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
TYPESAFE_URL = 'https://api.typesafe.ai/v1/systemone'
DEFAULT_MODEL = 'jev-latest'
DEFAULT_RUBRIC = Path(__file__).parent / 'rubrics' / 'system_design.json'
RETRYABLE = (429, 529)  # documented: back off and retry


class JudgeError(Exception):
    """The judge could not answer. Carries a message that is safe to store."""


# --- rubric -------------------------------------------------------------------------

def load_rubric(path):
    rubric = json.loads(Path(path).read_text(encoding='utf-8'))
    dims, vetoes = rubric.get('dimensions') or [], rubric.get('vetoes') or []
    if not dims:
        raise ValueError('rubric needs at least one dimension')
    ids = [d.get('id') for d in dims] + [v.get('id') for v in vetoes]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError('dimension and veto ids must be unique non-empty strings')
    for d in dims:
        if not 2 <= len(d.get('levels') or []) <= 10:
            raise ValueError(f"{d['id']}: a Score takes 2 to 10 levels")
        if not isinstance(d.get('weight'), (int, float)) or d['weight'] <= 0:
            raise ValueError(f"{d['id']}: weight must be a positive number")
        if not d.get('instructions'):
            raise ValueError(f"{d['id']}: instructions are required")
    for v in vetoes:
        if not v.get('instructions') or not 0 < v.get('threshold', 0) <= 1:
            raise ValueError(f"{v.get('id')}: veto needs instructions and a threshold in (0, 1]")
    decision = rubric.setdefault('decision', {})
    decision.setdefault('min_confidence', 0.5)
    decision.setdefault('min_margin', 0.05)
    return rubric


def build_questions(rubric):
    """Rubric -> the API's `questions` map. Ids are for code; the model never sees them."""
    questions = {}
    for d in rubric['dimensions']:
        questions[d['id']] = {'type': 'score', 'instructions': d['instructions'], 'criteria': list(d['levels'])}
    for v in rubric.get('vetoes', []):
        q = {'type': 'noul', 'instructions': v['instructions']}
        if v.get('criteria'):
            q['criteria'] = v['criteria']
        questions[v['id']] = q
    return questions


def build_request(brief, proposal, rubric, model=DEFAULT_MODEL):
    """Exactly what is sent for one proposal. Every proposal gets the same questions
    over the same brief, independently, so scores are comparable across proposals."""
    return {'state': {'decision_brief': brief, 'proposal': proposal},
            'model': model, 'questions': build_questions(rubric)}


# --- anonymisation ------------------------------------------------------------------

def anonymise(proposals, seed=0):
    """[(source_name, text)] -> [{'label','source','text'}], shuffled. The judge sees
    only the text; the label-to-source map stays in the report for the human."""
    if len(proposals) > len(string.ascii_uppercase):
        raise ValueError('at most 26 proposals')
    order = list(proposals)
    random.Random(seed).shuffle(order)
    return [{'label': f'Proposal {string.ascii_uppercase[i]}', 'source': name, 'text': text}
            for i, (name, text) in enumerate(order)]


# --- judges -------------------------------------------------------------------------

class TypeSafeJudge:
    name = 'typesafe'

    def __init__(self, api_key=None, url=TYPESAFE_URL, model=DEFAULT_MODEL, timeout_s=60.0,
                 max_attempts=4, opener=urllib.request.urlopen, sleep=time.sleep):
        self.api_key = api_key or os.environ.get('TYPESAFE_API_KEY')
        if not self.api_key:
            raise JudgeError('TYPESAFE_API_KEY is not set')
        self.url, self.model, self.timeout_s = url, model, timeout_s
        self.max_attempts, self.opener, self.sleep = max_attempts, opener, sleep

    def judge(self, brief, proposal, rubric):
        body = json.dumps(build_request(brief, proposal, rubric, self.model)).encode()
        request = urllib.request.Request(self.url, data=body, method='POST', headers={
            'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'})
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self.opener(request, timeout=self.timeout_s) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in RETRYABLE and attempt < self.max_attempts:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise JudgeError(f'TypeSafe API returned HTTP {exc.code}') from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise JudgeError(f'TypeSafe API unreachable: {type(exc).__name__}') from None
            except json.JSONDecodeError:
                raise JudgeError('TypeSafe API returned a body that is not JSON') from None
        answers = payload.get('answers') if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            raise JudgeError('TypeSafe response has no answers map')
        return {'answers': answers, 'model': payload.get('model'), 'usage': payload.get('usage')}


class OllamaJudge:
    """Offline stand-in so the pipeline runs with no key and nothing leaving the machine.
    An LLM forced to a JSON schema picks one level per dimension: no probability
    distribution, so confidence is None and the confidence gate reports 'unavailable'.
    Use it to check plumbing and as a second opinion, not as a calibrated judge."""
    name = 'ollama'

    def __init__(self, url='http://localhost:11434', model='qwen2.5:7b', timeout_s=300.0,
                 opener=urllib.request.urlopen):
        self.url, self.model, self.timeout_s, self.opener = url.rstrip('/'), model, timeout_s, opener

    def judge(self, brief, proposal, rubric):
        props, lines = {}, []
        for d in rubric['dimensions']:
            props[d['id']] = {'type': 'integer', 'minimum': 0, 'maximum': len(d['levels']) - 1}
            levels = ' | '.join(f'{i}: {text}' for i, text in enumerate(d['levels']))
            lines.append(f"{d['id']} (integer level): {d['instructions']} Levels -> {levels}")
        for v in rubric.get('vetoes', []):
            props[v['id']] = {'type': 'boolean'}
            lines.append(f"{v['id']} (true/false): {v['instructions']}")
        system = ('You are a strict evaluator of engineering design proposals. `decision_brief` and '
                  '`proposal` are given as JSON. Answer every item independently and literally. '
                  'Reply only with JSON.\n' + '\n'.join(lines))
        body = json.dumps({
            'model': self.model, 'stream': False, 'options': {'temperature': 0},
            'format': {'type': 'object', 'properties': props, 'required': list(props)},
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': json.dumps({'decision_brief': brief, 'proposal': proposal})}],
        }).encode()
        request = urllib.request.Request(self.url + '/api/chat', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                picked = json.loads(json.loads(response.read())['message']['content'])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JudgeError(f'Ollama unreachable: {type(exc).__name__}') from None
        except (KeyError, TypeError, json.JSONDecodeError):
            raise JudgeError('Ollama reply was not the requested JSON') from None
        answers = {}
        for d in rubric['dimensions']:
            level = picked.get(d['id'])
            if type(level) is not int or not 0 <= level < len(d['levels']):
                raise JudgeError(f"Ollama gave no valid level for {d['id']}")
            answers[d['id']] = {'type': 'score', 'score': float(level), 'confidence': None,
                                'probabilities': {str(i): float(i == level) for i in range(len(d['levels']))}}
        for v in rubric.get('vetoes', []):
            answers[v['id']] = {'type': 'noul', 'noul': 1.0 if picked.get(v['id']) is True else 0.0}
        return {'answers': answers, 'model': f'ollama:{self.model}', 'usage': None}


# --- the chairman is code -----------------------------------------------------------

def judge_all(brief, anonymised, rubric, judge):
    rows = []
    for item in anonymised:
        row = {'label': item['label'], 'source': item['source']}
        try:
            row.update(judge.judge(brief, item['text'], rubric))
        except JudgeError as exc:
            row['error'] = str(exc)
        rows.append(row)
    return rows


def score_row(row, rubric):
    """One judged proposal -> normalised dimensions, weighted total, vetoes, confidence."""
    if row.get('error'):
        return {'label': row['label'], 'source': row['source'], 'error': row['error']}
    answers, total_weight = row['answers'], sum(d['weight'] for d in rubric['dimensions'])
    dims, total, confidences, problems = {}, 0.0, [], []
    for d in rubric['dimensions']:
        answer = answers.get(d['id'])
        if not isinstance(answer, dict) or not isinstance(answer.get('score'), (int, float)):
            problems.append(f"no score for {d['id']}")
            continue
        top = len(d['levels']) - 1
        normalised = min(max(answer['score'] / top, 0.0), 1.0)
        dims[d['id']] = {'normalised': round(normalised, 4), 'confidence': answer.get('confidence')}
        total += normalised * d['weight'] / total_weight
        if isinstance(answer.get('confidence'), (int, float)):
            confidences.append(answer['confidence'])
    vetoes = []
    for v in rubric.get('vetoes', []):
        answer = answers.get(v['id'])
        if not isinstance(answer, dict) or not isinstance(answer.get('noul'), (int, float)):
            problems.append(f"no answer for veto {v['id']}")
        elif answer['noul'] >= v['threshold']:
            vetoes.append({'id': v['id'], 'probability': answer['noul'], 'threshold': v['threshold']})
    if problems:
        return {'label': row['label'], 'source': row['source'], 'error': '; '.join(problems)}
    return {'label': row['label'], 'source': row['source'], 'total': round(total, 4), 'dimensions': dims,
            'vetoes': vetoes, 'min_confidence': min(confidences) if confidences else None}


def decide(scored, rubric):
    """Recommend only when the evidence is clear; otherwise hand it to a person and
    say exactly why. A recommendation is advice. Nothing here acts on it."""
    rule, reasons = rubric['decision'], []
    for s in scored:
        if s.get('error'):
            reasons.append(f"{s['label']} could not be judged: {s['error']}")
        for v in s.get('vetoes', []):
            reasons.append(f"{s['label']} vetoed by {v['id']} (p={v['probability']:.2f} >= {v['threshold']})")
    eligible = sorted((s for s in scored if not s.get('error') and not s['vetoes']),
                      key=lambda s: s['total'], reverse=True)
    if not eligible:
        return {'action': 'escalate', 'recommendation': None, 'reasons': reasons + ['no proposal survived judging']}
    winner, blockers = eligible[0], []
    if any(s.get('error') for s in scored):
        blockers.append('at least one proposal was not judged, so the comparison is incomplete')
    if len(eligible) > 1:
        margin = round(winner['total'] - eligible[1]['total'], 4)
        if margin < rule['min_margin']:
            blockers.append(f"{winner['label']} leads {eligible[1]['label']} by {margin}, below min_margin {rule['min_margin']}")
    else:
        margin = None
    if winner['min_confidence'] is None:
        reasons.append('this judge reports no confidence, so the confidence gate was not applied')
    elif winner['min_confidence'] < rule['min_confidence']:
        blockers.append(f"judge confidence on {winner['label']} drops to {winner['min_confidence']:.2f}, "
                        f"below min_confidence {rule['min_confidence']}")
    return {'action': 'escalate' if blockers else 'recommend', 'recommendation': winner['label'],
            'recommended_source': winner['source'], 'margin': margin, 'reasons': reasons + blockers}


def build_report(brief, judged, rubric, judge_name, seed):
    scored = [score_row(row, rubric) for row in judged]
    return {'schema_version': SCHEMA_VERSION, 'judge': judge_name, 'seed': seed,
            'brief_sha256': hashlib.sha256(brief.encode()).hexdigest(),
            'rubric': rubric, 'judged': judged, 'scored': scored, 'decision': decide(scored, rubric)}


def rescore(report, rubric):
    """New weights or thresholds over the SAME raw answers. No judge call. The new
    rubric must keep the same question ids, or the stored answers would not apply."""
    old = {q for q in build_questions(report['rubric'])}
    if {q for q in build_questions(rubric)} != old:
        raise ValueError('rescore needs the same dimension and veto ids as the original run')
    scored = [score_row(row, rubric) for row in report['judged']]
    return {**report, 'rubric': rubric, 'scored': scored, 'decision': decide(scored, rubric)}


# --- CLI ----------------------------------------------------------------------------

def render(report):
    dims = [d['id'] for d in report['rubric']['dimensions']]
    lines = [f"judge: {report['judge']}   seed: {report['seed']}", '']
    header = f"{'':12}{'total':>7} " + ' '.join(f'{d[:12]:>13}' for d in dims) + '   source'
    lines.append(header)
    for s in sorted(report['scored'], key=lambda s: s.get('total', -1), reverse=True):
        if s.get('error'):
            lines.append(f"{s['label']:12}{'error':>7}  {s['error']}")
            continue
        cells = ' '.join(f"{s['dimensions'][d]['normalised']:>13.2f}" for d in dims)
        flag = '  VETOED' if s['vetoes'] else ''
        lines.append(f"{s['label']:12}{s['total']:>7.3f} {cells}   {s['source']}{flag}")
    d = report['decision']
    verdict = (f"RECOMMEND {d['recommendation']} ({d['recommended_source']})" if d['action'] == 'recommend'
               else f"ESCALATE TO A HUMAN" + (f" (leader: {d['recommendation']})" if d['recommendation'] else ''))
    lines += ['', verdict] + [f'  - {r}' for r in d['reasons']]
    return '\n'.join(lines)


def read_proposals(paths):
    return [(Path(p).name, Path(p).read_text(encoding='utf-8').strip()) for p in paths]


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('judge', 'show-request'):
        p = sub.add_parser(name)
        p.add_argument('--brief', required=True)
        p.add_argument('--proposals', nargs='+', required=True)
        p.add_argument('--rubric', default=str(DEFAULT_RUBRIC))
        p.add_argument('--seed', type=int, default=0)
        if name == 'judge':
            p.add_argument('--judge', choices=('typesafe', 'ollama'), default='typesafe')
            p.add_argument('--ollama-url', default='http://localhost:11434')
            p.add_argument('--ollama-model', default='qwen2.5:7b')
            p.add_argument('--out', help='write the full JSON report here')
    p = sub.add_parser('rescore')
    p.add_argument('--report', required=True)
    p.add_argument('--rubric', required=True)
    args = parser.parse_args(argv)

    if args.command == 'rescore':
        report = rescore(json.loads(Path(args.report).read_text(encoding='utf-8')), load_rubric(args.rubric))
        print(render(report), file=out)
        return 0

    rubric = load_rubric(args.rubric)
    brief = Path(args.brief).read_text(encoding='utf-8').strip()
    anonymised = anonymise(read_proposals(args.proposals), args.seed)
    if args.command == 'show-request':
        for item in anonymised:
            print(f"# {item['label']}  <- {item['source']}", file=out)
            print(json.dumps(build_request(brief, item['text'], rubric), indent=2), file=out)
        return 0
    try:
        judge = TypeSafeJudge() if args.judge == 'typesafe' else OllamaJudge(args.ollama_url, args.ollama_model)
    except JudgeError as exc:
        print(f'cannot start judge: {exc}', file=sys.stderr)
        return 2
    report = build_report(brief, judge_all(brief, anonymised, rubric, judge), rubric, judge.name, args.seed)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(render(report), file=out)
    return 0 if report['decision']['action'] == 'recommend' else 1


if __name__ == '__main__':
    raise SystemExit(main())
