"""Design council: several proposals in, one ADVISORY recommendation out.

After Karpathy's llm-council, with one change of shape. There, LLMs answer, LLMs
rank each other in prose, and a chairman LLM writes the verdict. Here:

  1. proposals come from anywhere (you, Claude, Codex, a local model) as text files
  2. they are anonymised and shuffled, then a judge scores each one against an
     explicit rubric: typed Score questions per dimension (a per_constraint dimension
     asks one Noul per hard constraint in the brief instead), Noul questions for vetoes
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
import re
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 2  # 2: reports keep the brief's parsed hard constraints
TYPESAFE_URL = 'https://api.typesafe.ai/v1/systemone'
DEFAULT_MODEL = 'jev-latest'
DEFAULT_RUBRIC = Path(__file__).parent / 'rubrics' / 'system_design.json'
RETRYABLE = (429, 529)  # documented: back off and retry


class JudgeError(Exception):
    """The judge could not answer. Carries a message that is safe to store."""


def retry_delay_s(headers, attempt):
    """The server's numeric Retry-After when it sends one (capped at a minute),
    otherwise exponential backoff. A date-valued or missing header falls back."""
    try:
        seconds = float((headers or {}).get('Retry-After'))
    except (TypeError, ValueError):
        seconds = float('nan')
    if seconds >= 0:  # False for NaN
        return min(seconds, 60.0)
    return min(2 ** attempt, 30)


# --- rubric -------------------------------------------------------------------------

def load_rubric(path):
    """A dimension is either a Score (`instructions` + `levels`) or `per_constraint`: one
    Noul per hard constraint listed in the brief, combined in code."""
    rubric = json.loads(Path(path).read_text(encoding='utf-8'))
    dims, vetoes = rubric.get('dimensions') or [], rubric.get('vetoes') or []
    if not dims:
        raise ValueError('rubric needs at least one dimension')
    ids = [d.get('id') for d in dims] + [v.get('id') for v in vetoes]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i and '.' not in i for i in ids):
        raise ValueError('dimension and veto ids must be unique non-empty strings without dots')
    for d in dims:
        if not isinstance(d.get('weight'), (int, float)) or d['weight'] <= 0:
            raise ValueError(f"{d['id']}: weight must be a positive number")
        if 'per_constraint' in d:
            if 'levels' in d:
                raise ValueError(f"{d['id']}: use either levels or per_constraint, not both")
            question = (d['per_constraint'] or {}).get('question')
            if not isinstance(question, str) or '`constraint`' not in question:
                raise ValueError(f"{d['id']}: per_constraint needs a question that refers to `constraint`")
            continue
        if not 2 <= len(d.get('levels') or []) <= 10:
            raise ValueError(f"{d['id']}: a Score takes 2 to 10 levels")
        if not d.get('instructions'):
            raise ValueError(f"{d['id']}: instructions are required")
    for v in vetoes:
        if not v.get('instructions') or not 0 < v.get('threshold', 0) <= 1:
            raise ValueError(f"{v.get('id')}: veto needs instructions and a threshold in (0, 1]")
    decision = rubric.setdefault('decision', {})
    decision.setdefault('min_confidence', 0.5)
    decision.setdefault('min_margin', 0.05)
    return rubric


HEADING = re.compile(r'^\s{0,3}(?:#{1,6}\s*)?[*_]{0,2}\s*hard\s+constraints?\s*(?:\([^)]*\))?\s*[*_]{0,2}\s*:?'
                     r'\s*[*_]{0,2}\s*#*\s*$', re.IGNORECASE)
BULLET = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+(.*\S)\s*$')
THEMATIC_BREAK = re.compile(r'^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$')


def parse_constraints(brief):
    """The bullets under every `Hard constraints:` line or heading in the brief, in order,
    read the way Markdown reads a list. Text between the heading and the first bullet is
    skipped. A line straight under a bullet continues it, indented or not; a bullet
    indented deeper than the list folds into the one above; blank lines between bullets
    are fine. After a blank line an unindented line ends the list, and so does a heading
    or a horizontal rule. No such section gives []."""
    constraints, inside, indent, after_blank = [], False, None, False
    for line in brief.splitlines():
        if HEADING.match(line):
            inside, indent, after_blank = True, None, False
            continue
        if not inside:
            continue
        if not line.strip():
            after_blank = True
            continue
        bullet = None if THEMATIC_BREAK.match(line) else BULLET.match(line)
        if indent is None:
            if bullet:
                indent = len(bullet.group(1))
                constraints.append(bullet.group(2))
            elif line.lstrip().startswith('#'):
                inside = False
        elif THEMATIC_BREAK.match(line) or line.lstrip().startswith('#'):
            inside = False
        elif bullet and len(bullet.group(1)) <= indent:
            constraints.append(bullet.group(2))
        elif bullet:
            constraints[-1] += (' ' if constraints[-1].endswith(':') else '; ') + bullet.group(2)
        elif line[:1].isspace() or not after_blank:
            constraints[-1] += ' ' + line.strip()
        else:
            inside = False
        after_blank = False
    return [' '.join(c.split()) for c in constraints]


def per_constraint_ids(d, constraints):
    return [f"{d['id']}.{i}" for i in range(1, len(constraints) + 1)]


def build_questions(rubric, constraints=()):
    """Rubric -> the API's `questions` map. Ids are for code; the model never sees them.
    A per_constraint dimension becomes one Noul per constraint, with the constraint
    carried in structured instructions so the question can point at it by name."""
    questions = {}
    for d in rubric['dimensions']:
        if 'per_constraint' in d:
            spec = d['per_constraint']
            for qid, text in zip(per_constraint_ids(d, constraints), constraints):
                q = {'type': 'noul', 'instructions': {'constraint': text, 'question': spec['question']}}
                if spec.get('criteria'):
                    q['criteria'] = spec['criteria']
                questions[qid] = q
            continue
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
            'model': model, 'questions': build_questions(rubric, parse_constraints(brief))}


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
                    self.sleep(retry_delay_s(exc.headers, attempt))
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


def describe(instructions):
    """Structured instructions as one line of prose for a judge that takes a prompt."""
    if isinstance(instructions, str):
        return instructions
    rest = '; '.join(f'`{k}` is {json.dumps(v)}' for k, v in instructions.items() if k != 'question')
    return f"{instructions.get('question', '')} ({rest})"


class OllamaJudge:
    """Offline stand-in so the pipeline runs with no key and nothing leaving the machine.
    An LLM forced to a JSON schema picks one level per Score and true or false per Noul:
    no probability distribution, so every answer carries confidence None and the
    confidence gate reports 'unavailable'. Use it to check plumbing and as a second
    opinion, not as a calibrated judge."""
    name = 'ollama'

    def __init__(self, url='http://localhost:11434', model='qwen2.5:7b', timeout_s=300.0,
                 opener=urllib.request.urlopen):
        self.url, self.model, self.timeout_s, self.opener = url.rstrip('/'), model, timeout_s, opener

    def judge(self, brief, proposal, rubric):
        questions, props, lines = build_questions(rubric, parse_constraints(brief)), {}, []
        for qid, q in questions.items():
            if q['type'] == 'score':
                props[qid] = {'type': 'integer', 'minimum': 0, 'maximum': len(q['criteria']) - 1}
                levels = ' | '.join(f'{i}: {text}' for i, text in enumerate(q['criteria']))
                lines.append(f"{qid} (integer level): {describe(q['instructions'])} Levels -> {levels}")
            else:
                props[qid] = {'type': 'boolean'}
                rules = ''.join(f' {k} means: {v}' for k, v in (q.get('criteria') or {}).items())
                lines.append(f"{qid} (true/false): {describe(q['instructions'])}{rules}")
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
        for qid, q in questions.items():
            if q['type'] == 'noul':
                answers[qid] = {'type': 'noul', 'noul': 1.0 if picked.get(qid) is True else 0.0, 'confidence': None}
                continue
            level, top = picked.get(qid), len(q['criteria']) - 1
            if type(level) is not int or not 0 <= level <= top:
                raise JudgeError(f"Ollama gave no valid level for {qid}")
            answers[qid] = {'type': 'score', 'score': float(level), 'confidence': None,
                            'probabilities': {str(i): float(i == level) for i in range(top + 1)}}
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


def noul_confidence(answer):
    """|2p - 1|: the peak-based confidence TypeSafe's docs illustrate for a Choice, applied
    to a Noul's two outcomes. 0 at p = 0.5, 1 at p = 0 or 1. None when the judge says it
    cannot report uncertainty."""
    if 'confidence' in answer and answer['confidence'] is None:
        return None
    return round(abs(2 * answer['noul'] - 1), 4)


def score_per_constraint(d, answers, constraints, problems):
    """Mean probability that the proposal addresses each constraint (an expected share,
    so an unsure 0.5 counts half), with the least certain constraint as its confidence."""
    found = []
    for qid, text in zip(per_constraint_ids(d, constraints), constraints):
        answer = answers.get(qid)
        if not isinstance(answer, dict) or not isinstance(answer.get('noul'), (int, float)):
            problems.append(f'no answer for {qid}')
        else:
            found.append({'constraint': text, 'probability': answer['noul'], 'confidence': noul_confidence(answer)})
    if not constraints:
        problems.append(f"{d['id']} needs the brief's hard constraints and there are none")
    if len(found) < len(constraints) or not constraints:
        return None
    known = [c['confidence'] for c in found if c['confidence'] is not None]
    return {'normalised': round(sum(c['probability'] for c in found) / len(found), 4),
            'confidence': min(known) if known else None, 'constraints': found}


def score_row(row, rubric, constraints=()):
    """One judged proposal -> normalised dimensions, weighted total, vetoes, confidence."""
    if row.get('error'):
        return {'label': row['label'], 'source': row['source'], 'error': row['error']}
    answers, total_weight = row['answers'], sum(d['weight'] for d in rubric['dimensions'])
    dims, total, confidences, problems = {}, 0.0, [], []
    for d in rubric['dimensions']:
        if 'per_constraint' in d:
            dim = score_per_constraint(d, answers, constraints, problems)
            if dim is None:
                continue
        else:
            answer = answers.get(d['id'])
            if not isinstance(answer, dict) or not isinstance(answer.get('score'), (int, float)):
                problems.append(f"no score for {d['id']}")
                continue
            top = len(d['levels']) - 1
            dim = {'normalised': round(min(max(answer['score'] / top, 0.0), 1.0), 4),
                   'confidence': answer.get('confidence')}
        dims[d['id']] = dim
        total += dim['normalised'] * d['weight'] / total_weight
        if isinstance(dim['confidence'], (int, float)):
            confidences.append((dim['confidence'], d['id']))
    vetoes = []
    for v in rubric.get('vetoes', []):
        answer = answers.get(v['id'])
        if not isinstance(answer, dict) or not isinstance(answer.get('noul'), (int, float)):
            problems.append(f"no answer for veto {v['id']}")
        elif answer['noul'] >= v['threshold']:
            vetoes.append({'id': v['id'], 'probability': answer['noul'], 'threshold': v['threshold']})
    if problems:
        return {'label': row['label'], 'source': row['source'], 'error': '; '.join(problems)}
    lowest = min(confidences, key=lambda c: c[0]) if confidences else (None, None)
    return {'label': row['label'], 'source': row['source'], 'total': round(total, 4), 'dimensions': dims,
            'vetoes': vetoes, 'min_confidence': lowest[0], 'min_confidence_at': lowest[1]}


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
        at, detail = winner.get('min_confidence_at'), ''
        unclear = sorted((c for c in winner['dimensions'].get(at, {}).get('constraints', [])
                          if c['confidence'] is not None and c['confidence'] < rule['min_confidence']),
                         key=lambda c: c['confidence'])
        if unclear:
            detail = ': unclear whether it addresses ' + ', '.join(
                f"\"{c['constraint']}\" (p={c['probability']:.2f})" for c in unclear)
        blockers.append(f"judge confidence on {winner['label']} drops to {winner['min_confidence']:.2f}"
                        f"{f' on {at}' if at else ''}{detail}, below min_confidence {rule['min_confidence']}")
    return {'action': 'escalate' if blockers else 'recommend', 'recommendation': winner['label'],
            'recommended_source': winner['source'], 'margin': margin, 'reasons': reasons + blockers}


def build_report(brief, judged, rubric, judge_name, seed):
    constraints = parse_constraints(brief)
    scored = [score_row(row, rubric, constraints) for row in judged]
    return {'schema_version': SCHEMA_VERSION, 'judge': judge_name, 'seed': seed,
            'brief_sha256': hashlib.sha256(brief.encode()).hexdigest(), 'constraints': constraints,
            'rubric': rubric, 'judged': judged, 'scored': scored, 'decision': decide(scored, rubric)}


def rescore(report, rubric):
    """New weights or thresholds over the SAME raw answers. No judge call. The new
    rubric must ask exactly the same questions, or the stored answers would not apply:
    reworded levels or instructions need a new judge run."""
    constraints = report.get('constraints', [])  # schema 1 reports predate per-constraint questions
    if build_questions(rubric, constraints) != build_questions(report['rubric'], constraints):
        raise ValueError('rescore needs the same questions (ids, instructions and levels) as the original '
                         'run; only weights, thresholds and decision rules may change')
    scored = [score_row(row, rubric, constraints) for row in report['judged']]
    return {**report, 'rubric': rubric, 'scored': scored, 'decision': decide(scored, rubric)}


# --- CLI ----------------------------------------------------------------------------

def render(report):
    dims = [d['id'] for d in report['rubric']['dimensions']]
    # An alias such as jev-latest moves between releases; log the version that answered.
    models = sorted({row['model'] for row in report['judged'] if row.get('model')})
    tokens = sum((row.get('usage') or {}).get('input_tokens') or 0 for row in report['judged'])
    head = f"judge: {report['judge']}   model: {', '.join(models) or 'n/a'}   seed: {report['seed']}"
    lines = [head + (f'   input tokens: {tokens}' if tokens else ''), '']
    header = f"{'':12}{'total':>7} " + ' '.join(f'{d[:12]:>13}' for d in dims) + '   source'
    lines.append(header)
    for s in sorted(report['scored'], key=lambda s: s.get('total', -1), reverse=True):
        if s.get('error'):
            lines.append(f"{s['label']:12}{'error':>7}  {s['error']}")
            continue
        cells = ' '.join(f"{s['dimensions'][d]['normalised']:>13.2f}" for d in dims)
        flag = '  VETOED' if s['vetoes'] else ''
        lines.append(f"{s['label']:12}{s['total']:>7.3f} {cells}   {s['source']}{flag}")
    judged = [s for s in sorted(report['scored'], key=lambda s: s.get('total', -1), reverse=True) if not s.get('error')]
    for dim in report['rubric']['dimensions']:
        if 'per_constraint' not in dim or not judged or dim['id'] not in judged[0]['dimensions']:
            continue
        lines += ['', f"{dim['id']}: probability that each proposal addresses the constraint"]
        lines.append(f"{'':52}" + ''.join(f"{s['label'].split()[-1]:>6}" for s in judged))
        for i, text in enumerate(report.get('constraints', [])):
            short = text if len(text) <= 46 else text[:45] + '…'
            cells = ''.join(f"{s['dimensions'][dim['id']]['constraints'][i]['probability']:>6.2f}" for s in judged)
            lines.append(f'  {i + 1}. {short:<47}{cells}')
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
        p.add_argument('--typesafe-model', default=DEFAULT_MODEL,
                       help='alias or version; pin a version such as jev-1.13.0 once thresholds are tuned')
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
        return 0 if report['decision']['action'] == 'recommend' else 1

    rubric = load_rubric(args.rubric)
    brief = Path(args.brief).read_text(encoding='utf-8').strip()
    per_constraint = [d['id'] for d in rubric['dimensions'] if 'per_constraint' in d]
    if per_constraint and not parse_constraints(brief):
        parser.error(f"the rubric asks one question per hard constraint ({', '.join(per_constraint)}), but "
                     f"{args.brief} has no 'Hard constraints:' list of bullets")
    if per_constraint:
        print(f'note: read {len(parse_constraints(brief))} hard constraints from {args.brief}', file=sys.stderr)
    # A glob such as examples/x/*.md also matches the brief; it is never a proposal.
    proposals = [p for p in args.proposals if Path(p).resolve() != Path(args.brief).resolve()]
    if len(proposals) < len(args.proposals):
        print(f'note: {args.brief} is the brief, so it was left out of the proposals', file=sys.stderr)
    if not proposals:
        parser.error('no proposals left once the brief is excluded')
    anonymised = anonymise(read_proposals(proposals), args.seed)
    if args.command == 'show-request':
        for item in anonymised:
            print(f"# {item['label']}  <- {item['source']}", file=out)
            print(json.dumps(build_request(brief, item['text'], rubric, args.typesafe_model), indent=2), file=out)
        return 0
    try:
        judge = (TypeSafeJudge(model=args.typesafe_model) if args.judge == 'typesafe'
                 else OllamaJudge(args.ollama_url, args.ollama_model))
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
