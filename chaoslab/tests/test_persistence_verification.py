import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verifier.probes import PersistenceProber
from verifier.oracle import ApplicationPersistenceRule,RuleStatus


class PersistenceVerificationTests(unittest.TestCase):
    spec={'enabled':True,'create':{'method':'POST','expected_json_key':'id','port':8002},
          'read_path':'/projects/{id}','verify_fields':['id','name','owner_name']}
    record={'id':'proj-1','name':'before fault','owner_name':'Alice Chen'}

    def test_prepare_captures_preexisting_record_before_fault(self):
        with patch('verifier.probes.HttpProber.execute_functional_test',return_value={'passed':True,'parsed_json':self.record,'transaction_id':'proj-1'}) as run:
            seed=PersistenceProber.prepare('old-pod','chaoslab-demo',self.spec)
        self.assertTrue(seed['passed'])
        self.assertEqual(seed['expected'],self.record)
        self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args.args[2]['path'],'/projects/proj-1')

    def test_same_record_survives_but_missing_or_changed_data_fails(self):
        seed={'record_id':'proj-1','expected':self.record}
        for record,expected in ((self.record,True),({**self.record,'name':'changed'},False),
                                ({**self.record,'id':'new-record'},False),(None,False)):
            with self.subTest(record=record),patch('verifier.probes.HttpProber.execute_functional_test',return_value={'passed':record is not None,'parsed_json':record}):
                result=PersistenceProber.verify('replacement','chaoslab-demo',self.spec,seed)
                self.assertEqual(result['passed'],expected)
                oracle=ApplicationPersistenceRule().evaluate({'application':{'persistence':result}},{'application':{'persistence_check':self.spec}})
                self.assertEqual(oracle.status,RuleStatus.PASS if expected else RuleStatus.FAIL)

    def test_failed_baseline_creation_is_not_accepted(self):
        with patch('verifier.probes.HttpProber.execute_functional_test',return_value={'passed':False}) as run:
            self.assertFalse(PersistenceProber.prepare('pod','chaoslab-demo',self.spec)['passed'])
            run.assert_called_once()

    def test_old_experiments_skip_persistence_and_missing_evidence_fails(self):
        self.assertEqual(ApplicationPersistenceRule().evaluate({},{}).status,RuleStatus.NOT_APPLICABLE)
        config={'application':{'persistence_check':self.spec}}
        self.assertEqual(ApplicationPersistenceRule().evaluate({},config).status,RuleStatus.FAIL)
