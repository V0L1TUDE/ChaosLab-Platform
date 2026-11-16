import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verifier.telemetry import TelemetryClient
from verifier.oracle import TelemetryRecordedRule,TelemetryLatencyRule,TelemetryErrorSpikeRule,RuleStatus
from verifier.probes import ProbeDispatcher

TRACE='1'*32


def trace(identifier=TRACE,project=True,user=True,start=100):
    spans=[]
    if project:spans.append({'processID':'p','operationName':'POST /projects','duration':100,'startTime':start,'tags':[{'key':'span.kind','value':'server'}]})
    if user:spans.append({'processID':'u','operationName':'GET /users/{id}','duration':900000,'startTime':start+1,'tags':[{'key':'span.kind','value':'server'}]})
    return {'traceID':identifier,'processes':{'p':{'serviceName':'project-service'},'u':{'serviceName':'user-service'}},'spans':spans}


class TelemetryEvidenceTests(unittest.TestCase):
    config={'telemetry':{'enabled':True,'service_name':'project-service','required_services':['project-service','user-service'],'require_transaction_trace':True,'max_allowed_p95_ms':1000}}

    def test_target_latency_excludes_other_services_and_requires_shared_trace(self):
        result=TelemetryClient().analyze_traces([trace()],'project-service',['project-service','user-service'],trace_id=TRACE)
        self.assertEqual(result['latency_ms']['p95'],0.1)
        self.assertEqual(result['spans_count'],1)
        self.assertEqual(result['distributed_traces_count'],1)
        result['requested_trace_id']=TRACE
        self.assertEqual(TelemetryRecordedRule().evaluate({'telemetry':result},self.config).status,RuleStatus.PASS)

    def test_unrelated_traces_cannot_satisfy_distributed_transaction(self):
        result=TelemetryClient().analyze_traces([trace(user=False),trace(identifier='2'*32,project=False)],'project-service',['project-service','user-service'])
        result['requested_trace_id']=TRACE
        self.assertEqual(result['distributed_traces_count'],0)
        self.assertEqual(TelemetryRecordedRule().evaluate({'telemetry':result},self.config).status,RuleStatus.FAIL)

    def test_pre_recovery_and_wrong_transaction_traces_are_excluded(self):
        result=TelemetryClient().analyze_traces([trace(start=1),trace(identifier='2'*32,start=200)],'project-service',start_time_us=100,end_time_us=500,trace_id=TRACE)
        self.assertEqual(result['traces_count'],0)

    def test_backend_failure_and_empty_results_cannot_pass_latency(self):
        client=TelemetryClient()
        for outcome in (RuntimeError('offline'),[]):
            with self.subTest(outcome=outcome),patch.object(client,'query_traces',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=[]):
                result=client.get_service_telemetry('project-service')
                self.assertEqual(TelemetryLatencyRule().evaluate({'telemetry':result},self.config).status,RuleStatus.FAIL)
                if isinstance(outcome,Exception):self.assertFalse(result['available'])

    def test_functional_workflow_receives_a_correlated_trace_header(self):
        config={'application':{'enabled':True,'health_probe':{'type':'http'},'functional_check':{'type':'http','method':'POST'}},'telemetry':{'enabled':True}}
        with patch('verifier.probes.HttpProber.probe_health',return_value={'passed':True}),patch('verifier.probes.HttpProber.execute_functional_test',return_value={'passed':True}) as run:
            result=ProbeDispatcher.probe_application('pod','chaoslab-demo',config)
        header=run.call_args.args[2]['headers']['traceparent']
        self.assertEqual(header.split('-')[1],result['trace_id'])
        self.assertNotIn('headers',config['application']['functional_check'])


    def test_error_spike_uses_fault_window_not_healthy_transaction_window(self):
        config={'telemetry':{'enabled':True,'verify_error_spike':True}}
        rule=TelemetryErrorSpikeRule()
        self.assertEqual(rule.evaluate({'telemetry':{'fault_window_errors':0,'error_spans_count':5}},config).status,RuleStatus.FAIL)
        self.assertEqual(rule.evaluate({'telemetry':{'fault_window_errors':1,'error_spans_count':0}},config).status,RuleStatus.PASS)


    def test_exported_client_parent_matches_incoming_trace_context(self):
        config={'application':{'enabled':True,'health_probe':{'type':'http'},'functional_check':{'type':'http','method':'POST'}},
                'telemetry':{'enabled':True,'emit_client_span':True}}
        with patch('verifier.probes.HttpProber.probe_health',return_value={'passed':True}), \
             patch('verifier.probes.HttpProber.execute_functional_test',return_value={'passed':True}) as request, \
             patch('verifier.probes.export_verification_client_span',return_value={'attempted':True}) as export:
            result=ProbeDispatcher.probe_application('pod','chaoslab-demo',config)
        context=request.call_args.args[2]['headers']['traceparent'].split('-')
        self.assertEqual(export.call_args.args[2],context[1])
        self.assertEqual(export.call_args.args[3],context[2])
        self.assertEqual(result['trace_id'],context[1])
        self.assertLessEqual(export.call_args.args[4],export.call_args.args[5])
