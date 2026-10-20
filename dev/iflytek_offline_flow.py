"""Logical simulation of the APK's ordinary offline result-to-editor path.

The native recognizer is real; this module models selected Java callbacks and a
blank, simplified-Chinese editor, not Android scheduling, cursor heuristics, the
400 ms character animation, or a physical AudioRecord/InputConnection instance.
Source: EdgeEsrUtil.g/i/h -> ryf.a.j -> syf -> d4e/o6e/h4b.
"""
import re

from iflytek_esr_protocol import result_text

PUNCTUATION = '，。？！,.?!'


def candidate_branch_enabled(callback):
    """syf.q exits before candidateEngineProcessResult without cloud JSON."""
    return 'cloud' in callback


def normalize_identifiers(text):
    compact = text.replace(' ', '')
    if compact.isdigit() and compact.isascii():
        return compact
    upper = compact.upper()
    plate = (r'^(?:[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领A-Z]'
             r'[A-Z](?:[0-9]{5}[DF]|[DF][A-HJ-NP-Z0-9][0-9]{4})|'
             r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领A-Z]'
             r'[A-Z][A-HJ-NP-Z0-9]{4}[A-HJ-NP-Z0-9挂学警港澳])$')
    if len(compact) in (7, 8) and re.fullmatch(plate, upper):
        return upper
    if len(compact) == 17 and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', upper):
        return upper
    if len(compact) == 18 and re.fullmatch(r'[0-9ABCDEFGHJKLMNPQRTUWXY]{2}[0-9]{6}[0-9ABCDEFGHJKLMNPQRTUWXY]{10}', upper):
        return upper
    return text


class Editor:
    """h4b's stable prefix + replaceable tail, collapsed over its animation."""
    def __init__(self):
        self.stable, self.pending, self.committed = '', '', ''
        self.actions = []

    def accept(self, text, append, final):
        if append:
            self.stable += self.pending
            self.pending = ''
        if text:
            self.pending = text
            self.actions.append({'operation': 'setComposingText', 'text': self.stable+self.pending})
        if final:
            self.committed += self.stable+self.pending
            self.actions.append({'operation': 'endProgressiveComposing', 'text': self.committed})
            self.stable, self.pending = '', ''


class OfflineFlow:
    def __init__(self, editor_kind='other', punctuation_mode=0):
        if editor_kind not in ('other', 'keyword') or punctuation_mode not in (0, 2):
            raise ValueError('Only blank generic/keyword editors and punctuation modes 0/2 are modeled')
        self.editor_kind, self.punctuation_mode = editor_kind, punctuation_mode
        self.previous_replaceable = False
        self.editor, self.trace = Editor(), []
        self.first = True

    def on_native(self, event, *, input_seconds=0, elapsed_seconds=0):
        if event.get('error'):
            raise RuntimeError('Native engine error: '+str(event['error']))
        entries, origin_entries, controls = [], [], []
        for item in event.get('elems', []):
            kind, value = item.get('sub_key'), item.get('value', '')
            if kind not in ('plain', 'pgs') or not value:
                continue
            origin_entries.append(dict(item))
            if kind == 'pgs':
                body, _, suffix = value.partition('{')
                controls.extend({k: int(v)} for k,v in re.findall(r'(useCloud|nVadBegin)\s*:\s*(-?\d+)', suffix))
                entries.append((body.removeprefix('<s>'), False))
            else:
                entries.append((result_text(value), '{' in value and '}' in value))
        if not entries and event.get('status') != 2:
            return
        structured = any(flag for _,flag in entries)
        # EdgeEsrUtil.g concatenates structured plain results when available;
        # otherwise it keeps the longest tentative text. 112=2 preserves native
        # punctuation here; the editor punctuation processor runs below.
        text = ''.join(t for t,flag in entries if flag) if structured else max((t for t,_ in entries), key=len, default='')
        final = event.get('status') == 2
        callback = {'en': 'esr', 'result': text, 'estatus': event.get('status', 0),
            'epgs': 'apd' if structured else 'rpl',
            'pgs': 'rpl' if self.previous_replaceable else 'apd',
            'origin': {'status': event.get('status', 0), 'elems': origin_entries}}
        if controls:
            callback['control'] = controls
        self.previous_replaceable = not structured
        if candidate_branch_enabled(callback):
            raise AssertionError('An offline callback unexpectedly entered the cloud candidate branch')
        sms = {'mEngineType': 'esr', 'sSentence': text, 'rStatus': 5 if final else 0,
               'bDynamicWord': callback['pgs'] != 'rpl'}
        processed = normalize_identifiers(text)
        if self.first and processed:
            self.first = False
            if processed[0] in PUNCTUATION:
                processed = processed[1:]
        # o6e.C: keyword fields/mode 2 strip punctuation while preserving a
        # decimal point before a digit. Blank generic 'other'/recommended mode
        # removes one sentence-tail punctuation at final completion (o6e.B/h).
        if self.editor_kind == 'keyword' or self.punctuation_mode == 2:
            processed = re.sub(r'\.(?!\d)', '', processed)
            processed = re.sub(r'[，。！？,!?]', '', processed)
        elif final and processed and processed[-1] in PUNCTUATION:
            processed = processed[:-1]
        sms['sSentence'] = processed
        before = len(self.editor.actions)
        self.editor.accept(processed, sms['bDynamicWord'], final)
        self.trace.append({'input_seconds': input_seconds, 'elapsed_seconds': elapsed_seconds,
            'native_status': event.get('status'), 'edge_text': text, 'callback': callback,
            'candidate_stage': 'skipped: syf.q requires cloud', 'ai_stage': 'skipped: offline',
            'sms': sms, 'editor_actions': self.editor.actions[before:]})
