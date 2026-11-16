"""Stable OTLP query parsing and lifecycle of owned local forwards."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verifier.telemetry import TelemetryClient,_otlp_traces
import local_runtime


class TraceTransportTests(unittest.TestCase):
    def chunk(self,service,identifier='a'*32):
        return {'result':{'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':service}}]},
                                          'scopeSpans':[{'spans':[{'traceId':identifier,'spanId':'b'*16,'name':'POST /projects','kind':2,
                                                                 'startTimeUnixNano':'100000','endTimeUnixNano':'200000','attributes':[]}]}]}]}}

    def test_v3_stream_merges_services_and_preserves_timing(self):
        text='\n'.join(json.dumps(self.chunk(service)) for service in ('project-service','user-service'))
        normalized=_otlp_traces(text)
        self.assertEqual(len(normalized),1)
        self.assertEqual(len(normalized[0]['spans']),2)
        result=TelemetryClient().analyze_traces(normalized,'project-service',['project-service','user-service'])
        self.assertEqual(result['distributed_traces_count'],1)
        self.assertEqual(result['latency_ms']['p95'],0.1)

    def test_query_uses_stable_v3_trace_endpoint(self):
        raw=json.dumps(self.chunk('project-service')).encode()
        with patch('verifier.telemetry.urllib.request.urlopen',return_value=io.BytesIO(raw)) as request:
            result=TelemetryClient().query_traces('project-service',trace_id='a'*32)
        self.assertEqual(result[0]['traceID'],'a'*32)
        self.assertIn('/api/v3/traces/'+'a'*32,request.call_args.args[0])

    def test_stream_error_cannot_be_accepted_as_partial_success(self):
        text=json.dumps(self.chunk('project-service'))+'\n'+json.dumps({'error':{'message':'failed'}})
        with self.assertRaises(ValueError):_otlp_traces(text)

    def test_managed_forward_terminates_owned_process_and_restores_environment(self):
        import os
        response=Mock();response.status=200;response.read.return_value=b'{"services":[]}'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        process=Mock();process.poll.return_value=None
        with patch.object(local_runtime.socket,'socket') as socket,patch.object(local_runtime.subprocess,'Popen',return_value=process) as launch, \
             patch.object(local_runtime,'kubectl_binary',return_value='kubectl'),patch.object(local_runtime.urllib.request,'urlopen',return_value=response), \
             patch.dict(os.environ,{'CHAOSLAB_JAEGER_QUERY_URL':'previous'}):
            with self.assertRaisesRegex(RuntimeError,'after startup'):
                with local_runtime.managed_trace_connection(16686):
                    self.assertEqual(os.environ['CHAOSLAB_JAEGER_QUERY_URL'],'http://127.0.0.1:16686')
                    raise RuntimeError('after startup')
            self.assertEqual(os.environ['CHAOSLAB_JAEGER_QUERY_URL'],'previous')
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=5)
            self.assertIn('--address',launch.call_args.args[0])
            self.assertIn('127.0.0.1',launch.call_args.args[0])

    def test_occupied_forward_port_does_not_touch_foreign_processes(self):
        with patch.object(local_runtime.socket,'socket') as socket,patch.object(local_runtime.subprocess,'Popen') as launch:
            socket.return_value.__enter__.return_value.bind.side_effect=OSError('in use')
            with self.assertRaisesRegex(RuntimeError,'occupied'):
                with local_runtime.managed_trace_connection():pass
            launch.assert_not_called()
