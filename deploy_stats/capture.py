"""Extract final usage without changing a JSON or SSE response."""
import json

MAX_FRAME = 4 * 1024 * 1024
MAX_COUNT = 2**53 - 1
MAX_DATA_LINES = 4096
MAX_JSON_ESCAPES = 65536


def _too_complex(payload, limit):
    """Bound JSON structure and quote scanning without counting string contents."""
    if limit is None:
        return False
    if payload.count(92) > MAX_JSON_ESCAPES:
        return True
    if sum(payload.count(mark) for mark in b'{}[],:') <= limit:
        return False
    size, position, structural, strings = len(payload), 0, 0, 0
    while position < size:
        opening = payload.find(b'"', position)
        end = size if opening < 0 else opening
        structural += sum(payload.count(mark, position, end) for mark in b'{}[],:')
        if structural > limit:
            return True
        if opening < 0:
            break
        strings += 1
        if strings > limit:
            return True
        position = opening + 1
        while position < size:
            closing = payload.find(b'"', position)
            if closing < 0:
                return False  # The JSON parser will reject the unterminated string.
            index = closing - 1
            while index >= position and payload[index] == 92:
                index -= 1
            position = closing + 1
            if (closing - index - 1) % 2 == 0:
                break
    return False


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def _may_have_non_null(payload, field):
    name = b'"' + field + b'"'
    offset = 0
    for _ in range(1024):
        marker = payload.find(name, offset)
        if marker < 0:
            return False
        position = marker + len(name)
        while position < len(payload) and payload[position] in b' \t\r\n':
            position += 1
            if position - marker > 128:
                return True
        if position >= len(payload) or payload[position] != 58:
            return True
        position += 1
        while position < len(payload) and payload[position] in b' \t\r\n':
            position += 1
            if position - marker > 128:
                return True
        if payload[position:position + 4] != b'null':
            return True
        offset = position + 4
        if offset < len(payload) and payload[offset] not in b' \t\r\n,}]':
            return True
    return True


def _may_have_final_usage(protocol, payload, chat_finished):
    if protocol == 'chat' and not chat_finished and not _may_have_non_null(payload, b'finish_reason'):
        return False
    if protocol == 'responses' and b'response.completed' not in payload and b'response.incomplete' not in payload:
        return False
    if protocol == 'messages' and b'message_delta' not in payload:
        return False
    return _may_have_non_null(payload, b'usage')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def extract_usage(protocol, value, *, allow_partial=False):
    """Return inclusive input, output and cached counts, or None if unknown."""
    if not isinstance(value, dict):
        return None
    if protocol == 'responses' and isinstance(value.get('response'), dict):
        value = value['response']
    usage = value.get('usage')
    if not isinstance(usage, dict):
        return None
    if protocol == 'messages':
        uncached = _count(usage.get('input_tokens'))
        cached = _count(usage.get('cache_read_input_tokens'))
        created = _count(usage.get('cache_creation_input_tokens', 0))
        output = _count(usage.get('output_tokens'))
        if None in (uncached, cached, created, output):
            return None
        prompt = uncached + cached + created
    elif protocol == 'responses':
        prompt = _count(usage.get('input_tokens'))
        output = _count(usage.get('output_tokens'))
        details = usage.get('input_tokens_details')
        if details is not None and not isinstance(details, dict):
            return None
        cached = _count(details.get('cached_tokens')) if isinstance(details, dict) else None
        if isinstance(details, dict) and details.get('cached_tokens') is not None and cached is None:
            return None
    else:
        prompt = _count(usage.get('prompt_tokens'))
        output = _count(usage.get('completion_tokens'))
        details = usage.get('prompt_tokens_details')
        if details is not None and not isinstance(details, dict):
            return None
        cached = _count(details.get('cached_tokens')) if isinstance(details, dict) else None
        alternate = _count(usage.get('prompt_cache_hit_tokens'))
        if ((isinstance(details, dict) and details.get('cached_tokens') is not None and cached is None) or
                (usage.get('prompt_cache_hit_tokens') is not None and alternate is None)):
            return None
        if cached is None:
            cached = alternate
        elif alternate is not None and alternate != cached:
            return None
        miss = usage.get('prompt_cache_miss_tokens')
        if miss is not None and (_count(miss) is None or prompt is None or cached is None or miss != prompt - cached):
            return None
    if prompt is None or output is None or prompt > MAX_COUNT:
        return None
    if cached is None and (not allow_partial or protocol not in ('chat', 'responses')):
        return None
    if cached is not None and cached > prompt:
        return None
    total = usage.get('total_tokens')
    if total is not None and _count(total) != prompt + output:
        return None
    return prompt, output, cached


def has_visible_delta(protocol, value):
    """Recognize a nonempty output delta, not a role or lifecycle event."""
    if not isinstance(value, dict):
        return False
    if protocol == 'chat':
        choices = value.get('choices')
        for choice in choices if isinstance(choices, list) else ():
            delta = choice.get('delta') if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            if any(isinstance(delta.get(k), str) and delta[k] for k in ('content', 'reasoning', 'reasoning_content')):
                return True
            for call in delta.get('tool_calls', ()) if isinstance(delta.get('tool_calls'), list) else ():
                fn = call.get('function') if isinstance(call, dict) else None
                if isinstance(fn, dict) and any(isinstance(fn.get(k), str) and fn[k] for k in ('name', 'arguments')):
                    return True
        return False
    event_type = value.get('type')
    if protocol == 'responses':
        return (isinstance(event_type, str) and event_type.startswith('response.') and
                event_type.endswith('.delta') and isinstance(value.get('delta'), str) and bool(value['delta']))
    if protocol == 'messages' and event_type == 'content_block_delta':
        delta = value.get('delta')
        return isinstance(delta, dict) and any(isinstance(delta.get(k), str) and delta[k]
            for k in ('text', 'thinking', 'partial_json'))
    return False


class UsageCapture:
    """Bound memory and retain only numerical usage after each response frame."""

    def __init__(self, protocol, content_type, content_encoding='', on_first_output=None, *,
                 max_frame=MAX_FRAME, max_structure=None, max_delta=None, allow_partial=False):
        self.protocol = protocol
        self.allow_partial = allow_partial
        self.max_frame = max_frame
        self.max_structure = max_structure
        self.max_delta = max_delta
        media_type = (content_type or '').split(';', 1)[0].strip().lower()
        self.kind = ('sse' if media_type == 'text/event-stream' else
                     'json' if media_type == 'application/json' else 'other')
        if (content_encoding or '').strip().lower() not in ('', 'identity'):
            self.kind = 'other'
        self.buffer = bytearray()
        self.line_scan_start = 0
        self.event_data = bytearray()
        self.event_bytes = 0
        self.event_lines = 0
        self.discard_line = False
        self.discard_event = False
        self.too_large = False
        self.incomplete = False
        self.usage = None
        self.terminal = False
        self.on_first_output = on_first_output
        self.saw_output = False
        self.first_unavailable = False
        self.skipped = False
        self.chat_finished = False

    def feed(self, data):
        if self.kind == 'other':
            return
        if self.kind == 'json':
            if not self.too_large and len(self.buffer) + len(data) <= self.max_frame:
                self.buffer.extend(data)
            else:
                self.buffer.clear()
                self.too_large = True
            return
        if self.discard_line:
            end = data.find(b'\n')
            if end < 0:
                return
            data = data[end + 1:]
            self.discard_line = False
        self.buffer.extend(data)
        while True:
            end = self.buffer.find(b'\n', self.line_scan_start)
            if end < 0:
                if len(self.buffer) > self.max_frame:
                    self.buffer.clear()
                    self.line_scan_start = 0
                    self.event_data.clear()
                    self.event_bytes = 0
                    self.event_lines = 0
                    self.discard_line = True
                    self.discard_event = True
                    self.incomplete = True
                else:
                    self.line_scan_start = len(self.buffer)
                break
            line = bytes(self.buffer[:end]).removesuffix(b'\r')
            del self.buffer[:end + 1]
            self.line_scan_start = 0
            if self.discard_line:
                self.discard_line = False
                continue
            if not line:
                self._event()
            elif line.startswith(b'data:') and not self.discard_event:
                item = line[5:]
                if item.startswith(b' '):
                    item = item[1:]
                self.event_lines += 1
                self.event_bytes += len(item) + (self.event_lines > 1)
                if self.event_bytes > self.max_frame or self.event_lines > MAX_DATA_LINES:
                    self.discard_event = True
                    self.incomplete = True
                    self.event_data.clear()
                    self.event_bytes = 0
                    self.event_lines = 0
                else:
                    if self.event_lines > 1:
                        self.event_data.append(10)
                    self.event_data.extend(item)

    def _event(self):
        if not self.discard_event and self.event_lines:
            payload = self.event_data
            if payload == b'[DONE]':
                if self.protocol == 'chat':
                    self.terminal = True
                elif not self.terminal:
                    self.incomplete = True
            elif self.terminal:
                self.incomplete = True
            elif (self.max_delta is not None and len(payload) > self.max_delta and
                  not _may_have_final_usage(self.protocol, payload, self.chat_finished)):
                # A later valid final usage can still account for the request,
                # but skipped output cannot establish the first output time.
                self.usage = None
                self.skipped = True
                self.chat_finished = False
                if not self.saw_output:
                    self.first_unavailable = True
            else:
                if (self.protocol == 'chat' and self.max_delta is not None and len(payload) > self.max_delta
                        and not self.chat_finished):
                    self.usage = None
                    self.skipped = True
                value = self._decode(payload)
                if not isinstance(value, dict):
                    self.incomplete = True
                else:
                    event_type = value.get('type')
                    # A prior usage snapshot is no longer final when generation
                    # continues. A later valid usage event can restore it.
                    if self.protocol == 'chat':
                        choices = value.get('choices')
                        self.usage = None
                    elif self.protocol == 'messages' and event_type != 'message_stop':
                        self.usage = None
                    elif self.protocol == 'messages' and value.get('usage') is not None:
                        self.incomplete = True
                    if self.protocol == 'chat':
                        if isinstance(choices, list) and any(
                                isinstance(choice, dict) and isinstance(choice.get('finish_reason'), str)
                                and choice['finish_reason'] for choice in choices):
                            self.chat_finished = True
                    if (not self.saw_output and not self.first_unavailable and not self.incomplete and
                            has_visible_delta(self.protocol, value)):
                        self.saw_output = True
                        if self.on_first_output:
                            self.on_first_output()
                    final = (self.protocol == 'chat' or
                             self.protocol == 'messages' and event_type == 'message_delta' or
                             self.protocol == 'responses' and event_type in
                             ('response.completed', 'response.incomplete'))
                    if final:
                        usage = extract_usage(self.protocol, value, allow_partial=self.allow_partial)
                        if usage is not None and (not self.skipped or self.protocol != 'chat' or self.chat_finished):
                            self.usage = usage
                            self.skipped = False
                        elif usage is None:
                            source = value.get('response') if self.protocol == 'responses' else value
                            if isinstance(source, dict) and source.get('usage') is not None:
                                self.incomplete = True
                    if (self.protocol == 'messages' and event_type == 'message_stop' or
                            self.protocol == 'responses' and event_type in
                            ('response.completed', 'response.incomplete')):
                        self.terminal = True
        self.event_data.clear()
        self.event_bytes = 0
        self.event_lines = 0
        self.discard_event = False

    def _decode(self, payload):
        if _too_complex(payload, self.max_structure):
            self.incomplete = True
            return None
        try:
            return json.loads(payload, object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeDecodeError):
            self.incomplete = True
            return None

    def finish(self):
        if self.kind == 'json' and not self.too_large:
            return extract_usage(self.protocol, self._decode(self.buffer), allow_partial=self.allow_partial)
        if (self.kind == 'sse' and self.terminal and not self.incomplete and not self.buffer and
                not self.event_lines and not self.discard_line and not self.discard_event):
            return self.usage
        return None


class SafeCapture:
    """A statistics parser error must never interrupt the user's response."""

    def __init__(self, protocol, content_type, content_encoding='', on_first_output=None, *,
                 max_frame=MAX_FRAME, max_structure=None, max_delta=None, allow_partial=False):
        self.reader = UsageCapture(protocol, content_type, content_encoding, on_first_output,
                                   max_frame=max_frame, max_structure=max_structure, max_delta=max_delta,
                                   allow_partial=allow_partial)
        self.failed = False

    def feed(self, data):
        if not self.failed:
            try:
                self.reader.feed(data)
            except Exception:
                self.failed = True

    def finish(self):
        if self.failed:
            return None
        try:
            return self.reader.finish()
        except Exception:
            self.failed = True
            return None
