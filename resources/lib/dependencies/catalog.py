"""Pinned artifacts and paths. No downloads or native library loads on import."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from ..native_platform import worker_architecture, launch_command


@lru_cache(maxsize=8)
def catalog(root, architecture=None):
    data = json.loads((Path(root) / 'resources/config/components.json').read_text(encoding='utf-8'))
    selected = architecture or worker_architecture()
    data['components'].update(data.pop('platform_components', {}).get(selected, {}))
    return data


def specification(root, identifier):
    return catalog(str(root), worker_architecture())['components'][identifier]


def display_label(identifier, spec):
    # Both regions share the existing artifact and installation receipt.
    return spec['label'].replace('CodeBuddy 国际版', 'CodeBuddy') if identifier == 'codebuddy' else spec['label']


def revision(spec):
    # Upload hints do not change installed content or invalidate existing receipts.
    content = dict(spec, assets=[{key: value for key, value in asset.items() if key not in ('filename', 'md5')}
                                 for asset in spec['assets']])
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def component_path(root, profile, identifier):
    spec = specification(root, identifier)
    return Path(profile) / 'components' / identifier / (spec['version'] + '-' + revision(spec))


def installed(root, profile, identifier):
    target = component_path(root, profile, identifier)
    try:
        receipt = json.loads((target / 'installed.json').read_text())
        if receipt['revision'] != revision(specification(root, identifier)):
            return False
        files = receipt['files']
        required = specification(root, identifier).get('required', list(files))
        return bool(files) and all(name in files and (target / name).is_file()
            and (target / name).stat().st_size == files[name]['size'] for name in required)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def require(root, profile, identifier):
    if not installed(root, profile, identifier):
        raise ValueError('缺少' + display_label(identifier, specification(root, identifier)) + '，请在设置中下载所需组件。')
    return component_path(root, profile, identifier)


def requirements(root, values):
    result = []
    if values.get('recognition_engine') == 'onnx':
        model = catalog(str(root))['asr_models'][values['onnx_model']]
        result += ['onnx-runtime', model['component']]
    if values.get('agent_backend') == 'codex' or values.get('recognition_engine') == 'codex':
        result.append('codex')
    if values.get('agent_backend') == 'codebuddy':
        result.append('codebuddy')
    if values.get('tts_enabled') == 'on':
        result += ['onnx-runtime', 'matcha-bilingual']
    return list(dict.fromkeys(result))


def missing(root, profile, values):
    return [name for name in requirements(root, values) if not installed(root, profile, name)]


def codex_binary(root, profile):
    return require(root, profile, 'codex') / 'codex-app-server'


def native_command(root, profile, worker):
    """Speech stays in a child process using the current CPU's runtime."""
    runtime = require(root, profile, 'onnx-runtime') / 'lib'
    bootstrap = Path(root) / 'resources/native' / worker_architecture() / 'bridge'
    return launch_command(root, bootstrap / worker, [runtime])


def preload_delay():
    """One minute after boot; restarting Kodi must not restart the minute."""
    try:
        return max(0, 60 - float(Path('/proc/uptime').read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return 60
