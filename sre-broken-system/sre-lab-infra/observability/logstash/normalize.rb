# Shared normalization for container JSON, Nginx, case JSON and event metadata.
require 'time'

def clean(value)
  case value
  when String
    value[0, 4000].gsub(/\bbearer\s+[A-Za-z0-9._~+\/-]+=*/i, 'Bearer [REDACTED]').gsub(/(password|passwd|token|authorization|api[_-]?key|secret)(["']?\s*[:=]\s*["']?)([^\s,;"'}]+)/i, '\1\2[REDACTED]')
  when Array
    value.first(12).map { |v| clean(v) }
  when Numeric, TrueClass, FalseClass, NilClass
    value
  else
    nil
  end
end

def filter(event)
  app = event.get('app')
  app = event.to_hash unless app.is_a?(Hash)
  kind = app['record_type'] || 'log'
  return [] unless ['log', 'history', 'event'].include?(kind)
  service = app['service_name'] || app['service'] || event.get('[kubernetes][container][name]')
  service = service['name'] if service.is_a?(Hash)
  nginx = event.get('nginx')
  service ||= 'nginx' if nginx.is_a?(Hash)
  return [] unless service.is_a?(String) && !service.strip.empty? && service.length <= 160
  stamp = app['timestamp'] || app['@timestamp'] || event.get('@timestamp')
  if nginx.is_a?(Hash) && nginx['timestamp']
    stamp = Time.strptime(nginx['timestamp'], '%d/%b/%Y:%H:%M:%S %z').iso8601
  end
  begin
    timestamp = LogStash::Timestamp.new(Time.iso8601(stamp.to_s))
  rescue ArgumentError
    return []
  end
  fields = case kind
  when 'history'
    return [] unless ['history_id', 'task_id', 'user_id', 'project_id'].all? { |k| app[k].is_a?(String) && !app[k].empty? }
    ['history_id', 'task_id', 'user_id', 'project_id', 'category', 'symptom', 'root_cause', 'summary', 'severity', 'tags', 'evidence_summary', 'evidence_ids', 'conclusion_status']
  when 'event'
    return [] unless ['event_id', 'task_id', 'user_id', 'project_id', 'event_type'].all? { |k| app[k].is_a?(String) && !app[k].empty? }
    ['event_id', 'task_id', 'user_id', 'project_id', 'event_type', 'message', 'severity', 'tags']
  else
    ['environment', 'level', 'message', 'trace_id', 'span_id', 'request_id', 'host', 'pod_name', 'namespace', 'exception_type', 'stack_trace', 'fault_mode', 'sql', 'sql_text', 'version']
  end
  output = {}
  fields.each { |key| output[key] = clean(app[key]) }
  if fields.include?('tags')
    output['tags'] = Array(output['tags']).select { |tag| tag.is_a?(String) }.first(12)
  end
  output['service'] = service.strip
  if kind == 'history'
    output['timestamp'] = timestamp.to_s
    output['category'] = (output['category'] || 'unknown').to_s.downcase
    output['severity'] = (output['severity'] || 'UNKNOWN').to_s.upcase
  else
    output['@timestamp'] = timestamp
    output['service_name'] = service.strip
    output['source'] = kind == 'event' ? 'diagnosis' : (nginx ? 'nginx' : 'application')
    if kind == 'log'
      output['level'] = (output['level'] || 'INFO').to_s.upcase
      output['pod_name'] ||= app['pod']
      output['environment'] ||= 'development'
      output['namespace'] ||= event.get('[kubernetes][namespace]')
    end
  end
  # Only allowlisted fields survive; request headers and arbitrary secrets are discarded.
  event.to_hash.keys.each { |key| event.remove(key) unless key == '@metadata' }
  output.each { |key, value| event.set(key, value) }
  event.set('[@metadata][route]', kind)
  [event]
end

test 'invalid application timestamp is rejected' do
  in_event { {'service_name' => 'demo', 'timestamp' => 'invalid', 'message' => 'drop'} }
  expect('no indexed event') { |events| events.empty? }
end

test 'secrets and arbitrary fields are removed' do
  in_event { {'app' => {'service' => 'demo', 'timestamp' => '2026-09-25T12:00:00Z', 'level' => 'error', 'message' => '"token": "private" password=private Authorization: Bearer private', 'password' => 'private'}} }
  expect('canonical safe log') do |events|
    events.size == 1 && events[0].get('level') == 'ERROR' && events[0].get('password').nil? && !events[0].get('message').include?('private')
  end
end

test 'history without owner is rejected' do
  in_event { {'record_type' => 'history', 'service' => 'demo', 'history_id' => 'one'} }
  expect('no ownerless history') { |events| events.empty? }
end

test 'event without tags routes safely' do
  in_event { {'record_type' => 'event', 'event_id' => '1', 'task_id' => 't', 'user_id' => 'u', 'project_id' => 'p', 'event_type' => 'diagnosis.completed', 'service_name' => 'demo', 'timestamp' => '2026-09-25T12:00:00Z'} }
  expect('event with empty tags') { |events| events.size == 1 && events[0].get('[@metadata][route]') == 'event' && events[0].get('tags') == [] }
end
