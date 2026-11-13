"""개정 파이프라인 입력·메타데이터 시험. 실제 모델 호출로 표시하지 않는다."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import arch_revision_job as W


class RevisionPipelineTests(unittest.TestCase):
    def test_full_contract_preserves_long_bodies(self):
        common = 'common-marker ' * 500
        diagram = 'diagram-marker ' * 400
        inp = {'diagram': 'D1', 'prompt': {'common': common, 'patterns': {'D1': diagram}}}
        self.assertEqual(W.full_contract(inp), common + '\n\n' + diagram)
        self.assertGreater(len(W.full_contract(inp)), 1500)

    def test_previous_image_hash_change_rejected_before_model(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            (p / 'image.png').write_bytes(b'actual-fixture')
            inp = {'previous': {'source_directory': folder, 'image_sha256': '0' * 64}}
            with self.assertRaisesRegex(ValueError, 'previous_image_hash_changed'):
                W.previous_inputs(inp)

    def test_source_route_drift_has_zero_transport_calls(self):
        sent = []
        with patch.object(W.J, 'TEXT_MODEL', 'unapproved'), patch.object(W.J, '_post', lambda *a: sent.append(a)):
            with self.assertRaisesRegex(ValueError, 'architecture_route_drift'):
                W.ask('fixture', [], 'spec', [])
        self.assertEqual(sent, [])

    def test_spec_receives_previous_image_and_verbatim_prompt_metadata(self):
        calls, sent = [], []
        prompt, image = 'complete rule body ' * 200, b'fixture-bytes'
        def transport(url, data, ctype, timeout):
            sent.append(json.loads(data.decode('utf-8')))
            return {'model': 'gpt-6-astra', 'id': 'resp_unit_revision', 'status': 'completed',
                    'output_text': json.dumps({'summary': 'unit-probe-only'})}
        with patch.object(W.J, '_post', transport):
            value = W.ask(prompt, [image], 'spec', calls)
        self.assertEqual(value['summary'], 'unit-probe-only')
        self.assertEqual(sent[0]['input'][0]['content'][0]['text'], prompt)
        self.assertEqual(len(sent[0]['input'][0]['content']), 2)
        self.assertEqual(calls[0]['actual_model'], 'gpt-6-astra')
        self.assertEqual(calls[0]['response_id'], 'resp_unit_revision')
        self.assertEqual(calls[0]['image_sha256'], [W.sha(image)])


if __name__ == '__main__':
    unittest.main()
