"""Extract final usage without changing a JSON or SSE response."""
import json

MAX_FRAME = 4 * 1024 * 1024
MAX_COUNT = 2**53 - 1


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def extract_usage(protocol, value):
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
        cached = _count(details.get('cached_tokens')) if isinstance(details, dict) else None
    else:
        prompt = _count(usage.get('prompt_tokens'))
        output = _count(usage.get('completion_tokens'))
        details = usage.get('prompt_tokens_details')
        cached = _count(details.get('cached_tokens')) if isinstance(details, dict) else None
        alternate = _count(usage.get('prompt_cache_hit_tokens'))
        if cached is None:
            cached = alternate
        elif alternate is not None and alternate != cached:
            return None
    if None in (prompt, output, cached) or prompt > MAX_COUNT or cached > prompt:
        return None
    total = usage.get('total_tokens')
    if total is not None and _count(total) != prompt + output:
        return None
    return prompt, output, cached


class UsageCapture:
    """Bound memory and retain only numerical usage after each response frame."""

    def __init__(self, protocol, content_type, content_encoding=''):
        self.protocol = protocol
        media_type = (content_type or '').split(';', 1)[0].strip().lower()
        self.kind = ('sse' if media_type == 'text/event-stream' else
                     'json' if media_type == 'application/json' else 'other')
        if (content_encoding or '').strip().lower() not in ('', 'identity'):
            self.kind = 'other'
        self.buffer = bytearray()
        self.event_data = []
        self.event_bytes = 0
        self.discard_line = False
        self.discard_event = False
        self.too_large = False
        self.usage = None
        self.terminal = False

    def feed(self, data):
        if self.kind == 'other':
            return
        if self.kind == 'json':
            if not self.too_large and len(self.buffer) + len(data) <= MAX_FRAME:
                self.buffer.extend(data)
            else:
                self.buffer.clear()
                self.too_large = True
            return
        self.buffer.extend(data)
        while True:
            end = self.buffer.find(b'\n')
            if end < 0:
                if len(self.buffer) > MAX_FRAME:
                    self.buffer.clear()
                    self.discard_line = True
                    self.discard_event = True
                break
            line = bytes(self.buffer[:end]).removesuffix(b'\r')
            del self.buffer[:end + 1]
            if self.discard_line:
                self.discard_line = False
                continue
            if not line:
                self._event()
            elif line.startswith(b'data:') and not self.discard_event:
                item = line[5:]
                if item.startswith(b' '):
                    item = item[1:]
                self.event_bytes += len(item)
                if self.event_bytes > MAX_FRAME:
                    self.discard_event = True
                    self.event_data.clear()
                else:
                    self.event_data.append(item)

    def _event(self):
        if not self.discard_event and self.event_data:
            payload = b'\n'.join(self.event_data)
            if payload == b'[DONE]':
                self.terminal = True
            elif b'"usage"' in payload or b'message_stop' in payload:
                try:
                    value = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    value = None
                if isinstance(value, dict):
                    event_type = value.get('type')
                    final = (self.protocol == 'chat' or
                             self.protocol == 'messages' and event_type == 'message_delta' or
                             self.protocol == 'responses' and event_type in
                             ('response.completed', 'response.incomplete'))
                    if final:
                        usage = extract_usage(self.protocol, value)
                        if usage is not None:
                            self.usage = usage
                    if (self.protocol == 'messages' and event_type == 'message_stop' or
                            self.protocol == 'responses' and event_type in
                            ('response.completed', 'response.incomplete')):
                        self.terminal = True
        self.event_data.clear()
        self.event_bytes = 0
        self.discard_event = False

    def finish(self):
        if self.kind == 'json' and not self.too_large:
            try:
                return extract_usage(self.protocol, json.loads(self.buffer))
            except (ValueError, UnicodeDecodeError):
                return None
        if self.kind == 'sse' and self.terminal:
            return self.usage
        return None


class SafeCapture:
    """A statistics parser error must never interrupt the user's response."""

    def __init__(self, protocol, content_type, content_encoding=''):
        self.reader = UsageCapture(protocol, content_type, content_encoding)
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
