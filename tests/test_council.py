"""No network: the TypeSafe API and Ollama are replaced by fake openers."""
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import council as c

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / 'examples' / 'coordinator-host'


def rubric():
    return c.load_rubric(c.DEFAULT_RUBRIC)


def answers(levels, vetoes=None, confidence=0.9):
    """levels: {dimension_id: score}; vetoes: {veto_id: probability}."""
    out = {k: {'type': 'score', 'score': v, 'confidence': confidence, 'probabilities': {}} for k, v in levels.items()}
    for v in rubric().get('vetoes', []):
        out[v['id']] = {'type': 'noul', 'noul': (vetoes or {}).get(v['id'], 0.02)}
    return out


def flat(score, **kw):
    return answers({d['id']: score for d in rubric()['dimensions']}, **kw)


class ScriptedJudge:
    name = 'scripted'

    def __init__(self, by_text):
        self.by_text, self.seen = by_text, []

    def judge(self, brief, proposal, rub):
        self.seen.append(proposal)
        result = self.by_text[proposal]
        if isinstance(result, Exception):
            raise result
        return {'answers': result, 'model': 'fake', 'usage': None}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError('u', code, 'x', headers or {}, io.BytesIO(b'{}'))


class RubricTests(unittest.TestCase):
    def test_default_rubric_is_valid_and_matches_the_api_contract(self):
        questions = c.build_questions(rubric())
        for d in rubric()['dimensions']:
            q = questions[d['id']]
            self.assertEqual(q['type'], 'score')
            self.assertIsInstance(q['criteria'], list)          # Score: ordered array of levels
            self.assertTrue(2 <= len(q['criteria']) <= 10)
            self.assertIn('`proposal`', q['instructions'])       # state paths are backticked
        for v in rubric()['vetoes']:
            self.assertEqual(questions[v['id']]['type'], 'noul')
            self.assertEqual(set(questions[v['id']]['criteria']), {'true', 'false'})
        self.assertAlmostEqual(sum(d['weight'] for d in rubric()['dimensions']), 1.0)

    def test_request_shape(self):
        req = c.build_request('the brief', 'the proposal', rubric())
        self.assertEqual(set(req), {'state', 'model', 'questions'})
        self.assertEqual(req['state'], {'decision_brief': 'the brief', 'proposal': 'the proposal'})
        self.assertEqual(req['model'], 'jev-latest')

    def test_bad_rubrics_are_rejected(self):
        good = json.loads(c.DEFAULT_RUBRIC.read_text())
        for mutate in (lambda r: r.update(dimensions=[]),
                       lambda r: r['dimensions'][0].update(levels=['only one']),
                       lambda r: r['dimensions'][0].update(weight=0),
                       lambda r: r['dimensions'][1].update(id=r['dimensions'][0]['id']),
                       lambda r: r['vetoes'][0].update(threshold=0)):
            broken = json.loads(json.dumps(good)); mutate(broken)
            with mock.patch.object(Path, 'read_text', return_value=json.dumps(broken)):
                with self.assertRaises(ValueError):
                    c.load_rubric('x')


class AnonymiseTests(unittest.TestCase):
    PROPOSALS = [('claude.md', 'aaa'), ('codex.md', 'bbb'), ('skyler.md', 'ccc')]

    def test_deterministic_shuffle_and_the_judge_never_sees_the_source(self):
        first, again = c.anonymise(self.PROPOSALS, seed=3), c.anonymise(self.PROPOSALS, seed=3)
        self.assertEqual(first, again)
        self.assertEqual([i['label'] for i in first], ['Proposal A', 'Proposal B', 'Proposal C'])
        self.assertEqual(sorted(i['source'] for i in first), ['claude.md', 'codex.md', 'skyler.md'])
        orders = {tuple(i['source'] for i in c.anonymise(self.PROPOSALS, seed=s)) for s in range(12)}
        self.assertGreater(len(orders), 1)                       # order really varies with the seed
        judge = ScriptedJudge({'aaa': flat(1), 'bbb': flat(2), 'ccc': flat(3)})
        c.judge_all('brief', first, rubric(), judge)
        self.assertFalse(any('.md' in text for text in judge.seen))


class DecisionTests(unittest.TestCase):
    def run_council(self, by_text, rub=None):
        rub = rub or rubric()
        items = c.anonymise([(f'{k}.md', k) for k in by_text], seed=0)
        return c.build_report('brief', c.judge_all('brief', items, rub, ScriptedJudge(by_text)), rub, 'scripted', 0)

    def test_weighted_total_is_normalised_per_dimension(self):
        top = {d['id']: len(d['levels']) - 1 for d in rubric()['dimensions']}
        report = self.run_council({'best': answers(top), 'worst': flat(0)})
        totals = {s['source']: s['total'] for s in report['scored']}
        self.assertEqual(totals, {'best.md': 1.0, 'worst.md': 0.0})
        self.assertEqual(report['decision']['action'], 'recommend')
        self.assertEqual(report['decision']['recommended_source'], 'best.md')

    def test_a_veto_removes_the_top_scorer(self):
        report = self.run_council({'flashy': flat(3, vetoes={'violates_hard_constraint': 0.93}), 'sound': flat(2)})
        d = report['decision']
        self.assertEqual((d['action'], d['recommended_source']), ('recommend', 'sound.md'))
        self.assertTrue(any('vetoed by violates_hard_constraint' in r for r in d['reasons']))

    def test_close_call_escalates(self):
        a = flat(2); b = flat(2); b['simplicity']['score'] = 2.2       # 0.15 * 0.2/3 = 0.01 apart
        d = self.run_council({'a': a, 'b': b})['decision']
        self.assertEqual(d['action'], 'escalate')
        self.assertTrue(any('below min_margin' in r for r in d['reasons']))

    def test_low_judge_confidence_escalates_even_with_a_clear_lead(self):
        d = self.run_council({'lead': flat(3, confidence=0.2), 'trail': flat(1)})['decision']
        self.assertEqual(d['action'], 'escalate')
        self.assertTrue(any('below min_confidence' in r for r in d['reasons']))

    def test_judge_failure_is_recorded_and_escalates(self):
        report = self.run_council({'ok': flat(3), 'broken': c.JudgeError('TypeSafe API returned HTTP 529')})
        self.assertEqual(report['decision']['action'], 'escalate')
        self.assertIn('HTTP 529', json.dumps(report['scored']))

    def test_everything_vetoed_recommends_nothing(self):
        d = self.run_council({'x': flat(3, vetoes={'leaves_decision_unanswered': 0.9})})['decision']
        self.assertEqual((d['action'], d['recommendation']), ('escalate', None))

    def test_missing_answer_is_an_error_not_a_zero(self):
        partial = flat(3); del partial['evidence']
        scored = self.run_council({'p': partial, 'q': flat(1)})['scored']
        self.assertIn('no score for evidence', next(s for s in scored if s['source'] == 'p.md')['error'])

    def test_rescore_changes_the_winner_without_calling_a_judge(self):
        careful = flat(1); careful['failure_handling']['score'] = 3
        simple = flat(1); simple['simplicity']['score'] = 3
        report = self.run_council({'careful': careful, 'simple': simple})
        self.assertEqual(report['decision']['recommended_source'], 'careful.md')
        reweighted = json.loads(json.dumps(rubric()))
        for d in reweighted['dimensions']:
            d['weight'] = 0.9 if d['id'] == 'simplicity' else 0.02
        with mock.patch.object(c.TypeSafeJudge, 'judge', side_effect=AssertionError('no judge call allowed')):
            again = c.rescore(report, reweighted)
        self.assertEqual(again['decision']['recommended_source'], 'simple.md')
        reworded = json.loads(json.dumps(reweighted))
        reworded['dimensions'][0]['levels'][0] = 'a different level the judge never saw'
        with self.assertRaisesRegex(ValueError, 'same questions'):
            c.rescore(report, reworded)
        reweighted['dimensions'][0]['id'] = 'renamed'
        with self.assertRaises(ValueError):
            c.rescore(report, reweighted)


class TypeSafeJudgeTests(unittest.TestCase):
    OK = json.dumps({'model': 'jev-1.13.0', 'answers': {'constraint_fit': {'type': 'score', 'score': 2.4}},
                     'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()

    def test_sends_the_documented_request(self):
        sent = {}

        def opener(request, timeout):
            sent.update(url=request.full_url, method=request.get_method(), auth=request.get_header('Authorization'),
                        body=json.loads(request.data), timeout=timeout)
            return Response(self.OK)

        result = c.TypeSafeJudge(api_key='sk-test', opener=opener).judge('b', 'p', rubric())
        self.assertEqual((sent['url'], sent['method'], sent['auth']),
                         ('https://api.typesafe.ai/v1/systemone', 'POST', 'Bearer sk-test'))
        self.assertEqual(sent['body'], c.build_request('b', 'p', rubric()))
        self.assertEqual(result['model'], 'jev-1.13.0')

    def test_retries_429_and_529_with_backoff_then_succeeds(self):
        replies, naps = [http_error(429), http_error(529), Response(self.OK)], []

        def opener(request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        c.TypeSafeJudge(api_key='k', opener=opener, sleep=naps.append).judge('b', 'p', rubric())
        self.assertEqual(naps, [2, 4])

    def test_honours_a_numeric_retry_after_and_ignores_a_date(self):
        replies = [http_error(429, {'Retry-After': '7'}), http_error(429, {'Retry-After': '600'}),
                   http_error(529, {'Retry-After': 'Wed, 23 Sep 2026 10:00:00 GMT'}), Response(self.OK)]
        naps = []

        def opener(request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        c.TypeSafeJudge(api_key='k', opener=opener, sleep=naps.append).judge('b', 'p', rubric())
        self.assertEqual(naps, [7.0, 60.0, 8])

    def test_401_and_422_fail_at_once_and_never_leak_the_key(self):
        for code in (401, 422):
            calls = []

            def opener(request, timeout, code=code):
                calls.append(1)
                raise http_error(code)

            with self.assertRaises(c.JudgeError) as caught:
                c.TypeSafeJudge(api_key='sk-SECRET', opener=opener, sleep=lambda s: None).judge('b', 'p', rubric())
            self.assertEqual(len(calls), 1)
            self.assertIn(str(code), str(caught.exception))
            self.assertNotIn('SECRET', str(caught.exception))

    def test_gives_up_after_max_attempts(self):
        def opener(request, timeout):
            raise http_error(429)

        with self.assertRaises(c.JudgeError):
            c.TypeSafeJudge(api_key='k', opener=opener, sleep=lambda s: None, max_attempts=3).judge('b', 'p', rubric())

    def test_unreachable_garbage_and_missing_key(self):
        def down(request, timeout):
            raise urllib.error.URLError('nope')

        with self.assertRaisesRegex(c.JudgeError, 'unreachable'):
            c.TypeSafeJudge(api_key='k', opener=down).judge('b', 'p', rubric())
        with self.assertRaisesRegex(c.JudgeError, 'not JSON'):
            c.TypeSafeJudge(api_key='k', opener=lambda r, timeout: Response(b'<html>')).judge('b', 'p', rubric())
        with self.assertRaisesRegex(c.JudgeError, 'no answers'):
            c.TypeSafeJudge(api_key='k', opener=lambda r, timeout: Response(b'{"oops":1}')).judge('b', 'p', rubric())
        with mock.patch.dict(c.os.environ, {}, clear=True), self.assertRaisesRegex(c.JudgeError, 'not set'):
            c.TypeSafeJudge()


class OllamaJudgeTests(unittest.TestCase):
    def reply(self, picked):
        return lambda request, timeout: Response(json.dumps({'message': {'content': json.dumps(picked)}}).encode())

    def test_levels_become_score_answers_with_no_confidence(self):
        picked = {d['id']: 2 for d in rubric()['dimensions']} | {v['id']: False for v in rubric()['vetoes']}
        picked['violates_hard_constraint'] = True
        out = c.OllamaJudge(opener=self.reply(picked)).judge('b', 'p', rubric())['answers']
        self.assertEqual((out['evidence']['score'], out['evidence']['confidence']), (2.0, None))
        self.assertEqual((out['violates_hard_constraint']['noul'], out['leaves_decision_unanswered']['noul']), (1.0, 0.0))

    def test_out_of_range_level_is_an_error(self):
        picked = {d['id']: 99 for d in rubric()['dimensions']}
        with self.assertRaises(c.JudgeError):
            c.OllamaJudge(opener=self.reply(picked)).judge('b', 'p', rubric())


class CliTests(unittest.TestCase):
    ARGS = ['--brief', str(EXAMPLE / 'brief.md'), '--proposals',
            *(str(EXAMPLE / n) for n in ('native-windows.md', 'wsl2.md', 'measure-then-decide.md'))]

    def test_show_request_prints_every_payload_and_touches_no_network(self):
        out = io.StringIO()
        with mock.patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('no network allowed')):
            self.assertEqual(c.main(['show-request', *self.ARGS], out=out), 0)
        text = out.getvalue()
        self.assertEqual(text.count('"model": "jev-latest"'), 3)
        self.assertIn('Remote access to the desktop is NOT configured', text)

    def test_a_glob_that_matches_the_brief_leaves_it_out(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(c.sys, 'stderr', err):
            self.assertEqual(c.main(['show-request', '--brief', str(EXAMPLE / 'brief.md'), '--proposals',
                                     *map(str, sorted(EXAMPLE.glob('*.md')))], out=out), 0)
        self.assertEqual(out.getvalue().count('"state"'), 3)
        self.assertNotIn('<- brief.md', out.getvalue())
        self.assertIn('left out of the proposals', err.getvalue())

    def test_show_request_can_pin_a_model_version(self):
        out = io.StringIO()
        self.assertEqual(c.main(['show-request', '--typesafe-model', 'jev-1.13.0', *self.ARGS], out=out), 0)
        self.assertEqual(out.getvalue().count('"model": "jev-1.13.0"'), 3)

    def test_report_names_the_model_that_answered_and_the_tokens_used(self):
        judged = [{'label': 'Proposal A', 'source': 'a.md', 'answers': flat(2), 'model': 'jev-1.13.0',
                   'usage': {'input_tokens': 900, 'output_tokens': 40}},
                  {'label': 'Proposal B', 'source': 'b.md', 'error': 'TypeSafe API returned HTTP 529'}]
        head = c.render(c.build_report('brief', judged, rubric(), 'typesafe', 0)).splitlines()[0]
        self.assertIn('model: jev-1.13.0', head)
        self.assertIn('input tokens: 900', head)

    def test_rescore_of_the_saved_live_run_needs_no_key_and_exits_like_judge(self):
        saved = str(EXAMPLE / 'live-run.jev-1.13.0.json')
        out = io.StringIO()
        with mock.patch.dict(c.os.environ, {}, clear=True), \
                mock.patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('no network allowed')):
            code = c.main(['rescore', '--report', saved, '--rubric', str(c.DEFAULT_RUBRIC)], out=out)
        report = json.loads(Path(saved).read_text(encoding='utf-8'))
        self.assertEqual(code, 0 if report['decision']['action'] == 'recommend' else 1)
        self.assertIn('model: jev-1.13.0', out.getvalue())

    def test_judge_without_a_key_exits_2_and_says_why(self):
        err = io.StringIO()
        with mock.patch.dict(c.os.environ, {}, clear=True), mock.patch.object(c.sys, 'stderr', err):
            self.assertEqual(c.main(['judge', *self.ARGS], out=io.StringIO()), 2)
        self.assertIn('TYPESAFE_API_KEY is not set', err.getvalue())


if __name__ == '__main__':
    unittest.main()
