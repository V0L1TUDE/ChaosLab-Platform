"""Jaeger query client. Connections are explicit; no hidden background forwards."""
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error
import urllib.parse

JAEGER_DEFAULT_URL = 'http://127.0.0.1:16686'


def ensure_jaeger_connection(base_url=JAEGER_DEFAULT_URL, namespace='default'):
    for path,key in (('/api/v3/services','services'),('/api/services','data')):
        try:
            with urllib.request.urlopen(base_url.rstrip('/')+path, timeout=2) as response:
                return response.status == 200 and isinstance(json.load(response).get(key), list)
        except urllib.error.HTTPError as error:
            if error.code == 404:continue
            return False
        except (OSError, ValueError, AttributeError):
            return False
    return False


def _otlp_traces(text):
    """Normalize the stable Jaeger v3 OTLP JSON stream for the evidence analyzer."""
    traces={}
    for line in text.splitlines():
        if not line.strip():continue
        envelope=json.loads(line)
        if 'error' in envelope:raise ValueError('Tracing API returned a stream error')
        result=envelope.get('result')
        if not isinstance(result,dict) or not isinstance(result.get('resourceSpans',[]),list):
            raise ValueError('Invalid v3 tracing response')
        for resource in result.get('resourceSpans',[]):
            attributes={a['key']:next(iter(a.get('value',{}).values()),None) for a in resource.get('resource',{}).get('attributes',[])}
            service=attributes.get('service.name')
            for scope in resource.get('scopeSpans',[]):
                for span in scope.get('spans',[]):
                    trace_id=span['traceId']
                    record=traces.setdefault(trace_id,{'traceID':trace_id,'processes':{},'spans':[]})
                    process=str(service or 'unknown')
                    record['processes'][process]={'serviceName':service}
                    start=int(span['startTimeUnixNano']);end=int(span['endTimeUnixNano'])
                    if end < start:raise ValueError('Invalid span duration')
                    tags=[{'key':a['key'],'value':next(iter(a.get('value',{}).values()),None)} for a in span.get('attributes',[])]
                    if span.get('kind') in (2,'SPAN_KIND_SERVER'):tags.append({'key':'span.kind','value':'server'})
                    if span.get('status',{}).get('code') in (2,'STATUS_CODE_ERROR'):tags.append({'key':'otel.status_code','value':'ERROR'})
                    record['spans'].append({'processID':process,'operationName':span.get('name',''),
                                            'startTime':start//1000,'duration':(end-start)/1000,
                                            'spanID':span.get('spanId'),'parentSpanID':span.get('parentSpanId'),'tags':tags})
    return list(traces.values())


def calculate_percentile(values, p):
    if not values:
        return 0.0
    ordered=sorted(values)
    index=(len(ordered)-1)*(p/100)
    lower=int(index)
    if lower+1 < len(ordered):
        return ordered[lower]+(index-lower)*(ordered[lower+1]-ordered[lower])
    return ordered[lower]


class TelemetryClient:
    def __init__(self, base_url=JAEGER_DEFAULT_URL, namespace='default'):
        self.base_url=base_url.rstrip('/')
        self.namespace=namespace

    def is_connected(self):
        return ensure_jaeger_connection(self.base_url,self.namespace)

    def query_traces(self,service_name,start_time_us=None,end_time_us=None,limit=50,trace_id=None):
        if trace_id:
            if not re.fullmatch(r'[0-9a-f]{32}',trace_id):raise ValueError('Invalid trace ID')
            route='/api/v3/traces/'+trace_id
            legacy='/api/traces/'+trace_id
        else:
            end=end_time_us if end_time_us is not None else int(time.time()*1_000_000)
            start=start_time_us if start_time_us is not None else end-60_000_000
            stamp=lambda value:datetime.fromtimestamp(value/1_000_000,timezone.utc).isoformat().replace('+00:00','Z')
            params={'query.serviceName':service_name,'query.startTimeMin':stamp(start),
                    'query.startTimeMax':stamp(end),'query.searchDepth':limit}
            route='/api/v3/traces?'+urllib.parse.urlencode(params)
            old={'service':service_name,'limit':limit,'start':start,'end':end}
            legacy='/api/traces?'+urllib.parse.urlencode(old)
        for path,is_v3 in ((route,True),(legacy,False)):
            try:
                with urllib.request.urlopen(self.base_url+path,timeout=5) as response:
                    text=response.read().decode('utf-8')
                if is_v3:return _otlp_traces(text)
                data=json.loads(text)
                if not isinstance(data,dict) or not isinstance(data.get('data'),list) or data.get('errors'):
                    raise ValueError('Invalid legacy tracing response')
                return data['data']
            except urllib.error.HTTPError as error:
                if error.code == 404:continue
                raise RuntimeError('Tracing query failed') from error
            except (OSError,ValueError) as error:
                raise RuntimeError('Tracing backend unavailable or returned invalid data') from error
        if trace_id:return []
        raise RuntimeError('No supported Jaeger query API is available')

    def analyze_traces(self,traces,target_service=None,required_services=None,start_time_us=None,end_time_us=None,trace_id=None):
        durations=[];target_count=0;errors=0;ids=[];operations=set();observed=set();distributed=[]
        for trace in traces:
            identifier=trace.get('traceID')
            if trace_id and identifier != trace_id:continue
            processes=trace.get('processes',{})
            spans=[];services=set()
            for span in trace.get('spans',[]):
                start=span.get('startTime',0)
                if start_time_us is not None and start < start_time_us:continue
                if end_time_us is not None and start > end_time_us:continue
                service=processes.get(span.get('processID'),{}).get('serviceName')
                if service:services.add(service)
                if target_service and service != target_service:continue
                spans.append(span)
            if not spans:continue
            if identifier:ids.append(identifier)
            observed.update(services)
            if required_services and set(required_services).issubset(services):distributed.append(identifier)
            target_count+=len(spans)
            server_spans=[s for s in spans if any(t.get('key')=='span.kind' and str(t.get('value')).lower()=='server' for t in s.get('tags',[]))]
            for span in server_spans or spans:
                durations.append(float(span.get('duration',0))/1000)
            for span in spans:
                operations.add(span.get('operationName',''))
                tags={tag.get('key'):tag.get('value') for tag in span.get('tags',[])}
                if (tags.get('error') is True or str(tags.get('error')).lower()=='true'
                    or str(tags.get('otel.status_code')).upper()=='ERROR'
                    or tags.get('rpc.grpc.status_code') not in (None,0,'0')
                    or str(tags.get('http.response.status_code', tags.get('http.status_code',''))).startswith('5')):
                    errors+=1
        return {'available':True,'traces_count':len(ids),'spans_count':target_count,
                'error_spans_count':errors,'error_rate_pct':round(errors/target_count*100,2) if target_count else 0.0,
                'latency_ms':{'min':round(min(durations),2) if durations else 0.0,
                              'avg':round(sum(durations)/len(durations),2) if durations else 0.0,
                              'p50':round(calculate_percentile(durations,50),2),
                              'p95':round(calculate_percentile(durations,95),2),
                              'max':round(max(durations),2) if durations else 0.0},
                'sample_trace_ids':ids[:5],'operations':sorted(operations),'services_observed':sorted(observed),
                'distributed_trace_ids':distributed[:5],'distributed_traces_count':len(distributed)}

    def get_service_telemetry(self,service_name,start_time_us=None,end_time_us=None,limit=50,trace_id=None,required_services=None):
        try:
            traces=self.query_traces(service_name,start_time_us,end_time_us,limit,trace_id)
            result=self.analyze_traces(traces,service_name,required_services,start_time_us,end_time_us,trace_id)
        except (RuntimeError,ValueError,TypeError,AttributeError,KeyError) as error:
            result=self.analyze_traces([],service_name)
            result.update({'available':False,'error':str(error)})
        result['service']=service_name
        if trace_id:result['requested_trace_id']=trace_id
        return result
