"""Framed PCM client for the isolated research-only EdgeESR worker."""
import json
import os
import select
import struct
import subprocess
import time


def result_text(value):
    """Match EdgeEsrUtil.i/h: one value can contain adjacent JSON objects."""
    if value.startswith('<s>'):
        value = value[3:]
    if '{' not in value and '}' not in value:
        return value
    decoder, offset, pieces = json.JSONDecoder(), 0, []
    while offset < len(value):
        if value[offset].isspace():
            offset += 1
            continue
        payload, offset = decoder.raw_decode(value, offset)
        words = payload.get('ws', [])
        if isinstance(words, dict):
            words = [words]
        for word in words:
            text = word.get('w', '')
            pieces.append(text[3:] if text.startswith('<s>') else text)
    return ''.join(pieces)


class Transcript:
    def __init__(self):
        self.segments = {}
        self.committed = ''
        self.events = []
        self.partial = None

    @property
    def text(self):
        if self.partial is not None:
            return self.committed+self.partial
        return self.committed+''.join(self.segments[key] for key in sorted(self.segments))

    def update(self, event):
        if not isinstance(event, dict):
            raise ValueError('Invalid EdgeESR result')
        if event.get('error'):
            raise RuntimeError('EdgeESR error: ' + str(event['error']))
        self.events.append(event)
        candidates = {}
        progressive = False
        for item in event.get('elems', []):
            kind, key = item.get('sub_key'), item.get('key')
            if kind == 'pgs':
                # Native PGS is a complete tentative string followed by controls
                # such as {useCloud:0}, not JSON and not an append-only delta.
                self.partial = item.get('value', '').split('{', 1)[0]
                if self.partial.startswith('<s>'):
                    self.partial = self.partial[3:]
                progressive = True
                continue
            if kind not in ('plain', 'readable') or not item.get('value'):
                continue
            if not isinstance(key, int):
                raise ValueError('Missing result segment key')
            rank = 0 if kind == 'plain' else 1
            if key in candidates and candidates[key][0] <= rank:
                continue
            text = result_text(item['value'])
            candidates[key] = rank, text
        if event.get('status') == 1 and candidates:
            # With VAD, status 1 commits a segment, then key=1 is reused for
            # later segments. It is not a revision of the preceding segment.
            self.committed += ''.join(value[1] for value in candidates.values())
            self.segments.clear()
            self.partial = None
            return
        if progressive and event.get('status') == 2 and candidates:
            # Final plain accompanying PGS completes the current uncommitted
            # segment. Already committed VAD segments must remain intact.
            self.segments.clear()
        if event.get('status') == 2:
            self.partial = None
        self.segments.update({key: item[1] for key, item in candidates.items()})


class Worker:
    def __init__(self, arguments, diagnostic, *, environment=None):
        self.buffer = bytearray()
        self.transcript = Transcript()
        self.native_messages = []
        self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=diagnostic, bufsize=0, start_new_session=True,
                                        env=environment)
        try:
            self.ready = self.read(120)
            if self.ready['event'] != 'ready' or self.ready['return_code']:
                raise RuntimeError('EdgeESR did not initialize')
        except BaseException:
            self.close()
            raise

    def _more(self, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError('EdgeESR response timed out')
        if select.select([self.process.stdout], [], [], .05)[0]:
            data = os.read(self.process.stdout.fileno(), 8192)
            if not data:
                raise EOFError('EdgeESR worker exited')
            self.buffer.extend(data)
            if len(self.buffer) > 4 * 1024 * 1024:
                raise ValueError('EdgeESR response exceeded size limit')

    def read(self, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            while b'\n' not in self.buffer:
                self._more(deadline)
            line, _, following = self.buffer.partition(b'\n')
            self.buffer = bytearray(following)
            if not line.startswith(b'IFLYTEK_EVENT '):
                self.native_messages.append(line.decode(errors='replace'))
                continue
            _, event, code, size = line.split()
            size = int(size)
            if size < 0 or size > 4 * 1024 * 1024:
                raise ValueError('Invalid EdgeESR payload length')
            while len(self.buffer) < size + 1:
                self._more(deadline)
            payload = bytes(self.buffer[:size])
            if self.buffer[size] != 10:
                raise ValueError('Invalid EdgeESR frame terminator')
            del self.buffer[:size + 1]
            self.last_packet = {'event':event.decode(),'raw_payload':payload.decode()}
            return {'event': event.decode(), 'return_code': int(code),
                    'payload': json.loads(payload) if payload else None}

    def exchange(self, data=b'', *, reset=False):
        expected = 'reset' if reset else 'audio' if data else 'finish'
        if reset:
            self.transcript = Transcript()
        payload = memoryview(struct.pack('<I', 0xffffffff if reset else len(data)) + data)
        deadline = time.monotonic() + 10
        while payload:
            if time.monotonic() >= deadline:
                raise TimeoutError('EdgeESR PCM write timed out')
            if select.select([], [self.process.stdin], [], .05)[1]:
                count = os.write(self.process.stdin.fileno(), payload[:8192])
                if not count:
                    raise EOFError('EdgeESR input closed')
                payload = payload[count:]
        while True:
            response = self.read()
            if response['return_code']:
                raise RuntimeError('EdgeESR %s returned %d' % (response['event'], response['return_code']))
            if response['event'] == 'result':
                self.transcript.update(response['payload'])
                continue
            if response['event'] != expected:
                raise ValueError('Unexpected EdgeESR response: ' + response['event'])
            if expected == 'finish' and not response['payload'].get('final'):
                raise RuntimeError('EdgeESR did not return a final result')
            return self.transcript.text

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write(struct.pack('<I', 0xfffffffe))
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
