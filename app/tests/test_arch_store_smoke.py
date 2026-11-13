"""스토어와 실제 현재 소스 계약의 작은 인계 회귀. 고객/모델/운영을 사용하지 않는다."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import arch_store
from arch_store import Store, StoreError


def pack():
    return {'id': 'fixture-master', 'version': 'fixture-v1', 'common': 'FULL COMMON '*300,
            'patterns': {'D1':'D1 FULL '*300, 'D2':'D2 FULL '*300}, 'status':'approved',
            'analysis':[{'actual_model':'gpt-6-astra','response_id':'resp_unit_store_fixture','fallback_used':False}],
            'sources':{'guide_sha256':hashlib.sha256((arch_store.SOURCE_ROOT/'ARCH-DRAWING-STANDARD.md').read_bytes()).hexdigest(),
                       'samples':[{'name':name,'sha256':hashlib.sha256((arch_store.SOURCE_ROOT/'samples'/name).read_bytes()).hexdigest()}
                                  for name in ('sample-full-infra.jpg','sample-network.jpg')]}}


class StoreContractSmoke(unittest.TestCase):
    def setUp(self):
        self.sources=tempfile.TemporaryDirectory()
        self.addCleanup(self.sources.cleanup)
        root=Path(self.sources.name)
        (root/'samples').mkdir()
        (root/'ARCH-DRAWING-STANDARD.md').write_text('명시적인 단위시험 전용 기준이며 고객 승인 템플릿이 아니다.',encoding='utf-8')
        for name in ('sample-full-infra.jpg','sample-network.jpg'):
            (root/'samples'/name).write_bytes(('unit-test-only-not-a-rendered-customer-artifact:'+name).encode('utf-8'))
        replacement=patch.object(arch_store,'SOURCE_ROOT',root)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_reservation_is_idempotent_and_failed_revision_never_promoted(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            stored=store.save_prompt(pack(),'fixture-admin')
            row,new=store.reserve(501,'AS-IS','D1','nonce001','표현만 변경','fixture-admin','/fixture/case','new')
            same,recreated=store.reserve(501,'AS-IS','D1','nonce001','표현만 변경','fixture-admin','/fixture/case','new')
            self.assertTrue(new)
            self.assertFalse(recreated)
            self.assertEqual(row['revision_id'],same['revision_id'])
            self.assertEqual(row['input']['prompt']['sha256'],stored['sha256'])
            self.assertEqual(row['input']['prompt']['common'],pack()['common'])
            store.finish(501,'AS-IS','D1',row['revision_id'],'failed',{}, {},'fixture_failure')
            self.assertIsNone(store.current_success(501,'AS-IS','D1'))
            self.assertEqual(store.get(501,'AS-IS','D1',row['revision_id'])['status'],'failed')

    def test_other_project_cannot_use_parent_and_legacy_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            store.save_prompt(pack(),'fixture-admin')
            legacy=Path(root)/'501/AS-IS_D1'
            legacy.mkdir(parents=True)
            (legacy/'image.png').write_bytes(b'legacy-original')
            row,_=store.reserve(501,'AS-IS','D1','nonce002','','fixture-admin','/fixture/case','new')
            with self.assertRaises(StoreError) as caught:
                store.reserve(502,'AS-IS','D1','nonce003','','fixture-admin','/fixture/case','new',row['revision_id'])
            self.assertEqual(caught.exception.status,404)
            self.assertEqual((legacy/'image.png').read_bytes(),b'legacy-original')

    def test_source_digest_mismatch_remains_a_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            value=pack();value['sources']['guide_sha256']='0'*64
            with self.assertRaises(StoreError) as caught:
                Store(root).save_prompt(value,'fixture-admin')
            self.assertEqual(caught.exception.code,'source_sha256_mismatch')


if __name__=='__main__':
    unittest.main()
