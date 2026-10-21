"""Generate the pinned download manifest from the previously verified upstream archives.

Run on the development workstation; no model is copied into the addon package.
"""
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {}
MODELS = {}


def artifact(path, url, files, trees=None):
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    with path.open('rb') as stream:
        md5 = hashlib.file_digest(stream, 'md5').hexdigest()
    result = {'url': url, 'filename': path.name, 'md5': md5,
              'bytes': path.stat().st_size, 'sha256': sha, 'format': 'tar', 'files': files}
    if trees: result['trees'] = trees
    with tarfile.open(path) as archive:
        result['expanded_bytes'] = sum(row.size for row in archive if row.isfile() and (
            row.name in files or any(row.name.startswith(prefix) for prefix in (trees or {}))))
    return result


def add_model(identifier, label, family, directory, names, streaming=False, memory=0):
    archive = next(base / (directory + '.tar.bz2') for base in (
        ROOT / '.cache/onnx-chinese-models', ROOT / '.cache/sensevoice', ROOT / '.cache/zipformer-onnx')
        if (base / (directory + '.tar.bz2')).is_file())
    component = 'asr-' + identifier
    asset = artifact(archive, 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/' + archive.name,
        {directory + '/' + name: name for name in names + ['tokens.txt']})
    COMPONENTS[component] = {'version': directory.rsplit('-', 3)[-3] + '-' + directory.rsplit('-', 2)[-2] + '-' + directory.rsplit('-', 1)[-1],
        'label': label, 'assets': [asset], 'required': names + ['tokens.txt']}
    MODELS[identifier] = {'label': label + (' · 实时' if streaming else ' · 整句') + '｜占用约' + str(memory) + 'M内存', 'family': family,
        'component': component, 'networks': names, 'streaming': streaming, 'threads': 2, 'memory_mib': memory}


def main():
    for row in [
        ('zipformer-2025', 'Zipformer 中文 2025 INT8', 'zipformer', 'sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30',
         ['encoder.int8.onnx', 'decoder.onnx', 'joiner.int8.onnx'], True, 320),
        ('zipformer-2025-fp16', 'Zipformer 中文 L 2025 FP16', 'zipformer', 'sherpa-onnx-streaming-zipformer-zh-fp16-2025-06-30',
         ['encoder.fp16.onnx', 'decoder.fp16.onnx', 'joiner.fp16.onnx'], True, 1000),
        ('zipformer-2025-fp32', 'Zipformer 中文 L 2025 FP32', 'zipformer', 'sherpa-onnx-streaming-zipformer-zh-2025-06-30',
         ['encoder.onnx', 'decoder.onnx', 'joiner.onnx'], True, 900),
        ('sensevoice', 'SenseVoice Small 多语种 INT8', 'sensevoice', 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17', ['model.int8.onnx'], False, 400),
        ('nano-ctc', 'FunASR Nano CTC INT8', 'sensevoice', 'sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17', ['model.int8.onnx'], False, 460),
        ('firered-ctc', 'FireRedASR2 CTC 中英 2026 INT8', 'fire_red_ctc', 'sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25', ['model.int8.onnx'], False, 1100),
    ]:
        add_model(*row)
    tts_cache = ROOT.parent / '.cache/voice-tts'
    runtime = 'sherpa-onnx-v1.13.8-linux-aarch64-shared-cpu'
    COMPONENTS['onnx-runtime'] = {'version': '1.13.8', 'label': 'sherpa-onnx 语音运行库',
        'assets': [artifact(tts_cache / (runtime + '.tar.bz2'),
            'https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/' + runtime + '.tar.bz2',
            {runtime + '/lib/' + name: 'lib/' + name for name in ('libonnxruntime.so', 'libsherpa-onnx-c-api.so')})],
        'required': ['lib/libonnxruntime.so', 'lib/libsherpa-onnx-c-api.so']}
    names = {'model-steps-3.onnx': 'model.onnx', 'tokens.txt': 'tokens.txt', 'lexicon.txt': 'lexicon.txt',
             'phone-zh.fst': 'phone.fst', 'date-zh.fst': 'date.fst', 'number-zh.fst': 'number.fst', 'README.md': 'README.md'}
    model_asset = artifact(tts_cache / 'matcha-icefall-zh-en.tar.bz2',
        'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-en.tar.bz2',
        {'matcha-icefall-zh-en/' + a: b for a,b in names.items()},
        {'matcha-icefall-zh-en/espeak-ng-data/': 'espeak-ng-data/'})
    vocos = tts_cache / 'vocos-16khz-univ.onnx'
    vocos_asset = {'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/' + vocos.name,
        'bytes': vocos.stat().st_size, 'expanded_bytes': vocos.stat().st_size, 'format': 'raw',
        'target': 'vocos.onnx', 'sha256': hashlib.sha256(vocos.read_bytes()).hexdigest(),
        'filename': vocos.name, 'md5': hashlib.md5(vocos.read_bytes()).hexdigest()}
    COMPONENTS['matcha-bilingual'] = {'version': '2025-10-10', 'label': 'Matcha 中英双语语音模型',
        'assets': [model_asset, vocos_asset], 'required': list(names.values()) + ['vocos.onnx', 'espeak-ng-data/phontab', 'espeak-ng-data/en_dict']}
    # Pin a verified release descriptor rather than silently regenerating the
    # official build and dropping the CE sandbox fixes or its bwrap dependency.
    COMPONENTS['codex'] = json.loads((ROOT / 'dev/codex_component.json').read_text())
    COMPONENTS['codebuddy'] = json.loads((ROOT / 'dev/codebuddy_component.json').read_text())
    target = ROOT / 'resources/config/components.json'
    platforms = json.loads((ROOT/'dev/components-linux_x86_64.json').read_text())
    target.write_text(json.dumps({'schema': 1, 'components': COMPONENTS, 'asr_models': MODELS,
        'platform_components': {'linux_x86_64': platforms}}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Pinned', len(COMPONENTS), 'components and', len(MODELS), 'ASR configurations')


if __name__ == '__main__': main()
