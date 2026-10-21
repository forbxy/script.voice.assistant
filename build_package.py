"""Build the small addon; optional agents, inference libraries and models stay in userdata."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def packaged(path):
    """Publish every resource asset; generated Python caches are not assets."""
    relative = path.relative_to(ROOT)
    if not path.is_file() or '__pycache__' in relative.parts or path.suffix == '.pyc':
        return False
    return True


def build():
    for asset in ('resources/icon-assistant-transparent.png',
                  'resources/lib/speech/service.py',
                  'resources/lib/data_paths.py',
                  'resources/lib/profile_migration.py',
                  'resources/lib/profile_upgrade.py',
                  'resources/lib/speech/output.py',
                  'resources/native/linux_aarch64/bridge/matcha-worker',
                  'resources/native/linux_aarch64/bridge/asr-worker',
                  'resources/native/linux_aarch64/bridge/lib/ld-linux-aarch64.so.1',
                  'resources/config/components.json',
                  'resources/licenses/matcha/ESPEAK-NG-COPYING',
                  'resources/licenses/matcha/build.json',
                  'resources/native/linux_armv7l/cmcc/libcmcc-siren-ffmpeg62.so',
                  'resources/native/linux_aarch64/cmcc/libcmcc-siren-ffmpeg63.so',
                  'resources/native/linux_armv7l/r22/libr22-msbc-ffmpeg62.so',
                  'resources/native/linux_aarch64/r22/libr22-msbc-ffmpeg63.so',
                  'resources/licenses/r22-build.json', 'resources/licenses/r22_msbc_decoder.c',
                  'resources/native/linux_armv7l/nanosic/libnanosic-wnvc-avutil60.so',
                  'resources/native/linux_aarch64/nanosic/libnanosic-wnvc-avutil61.so',
                  'resources/licenses/NANOSIC-DECODER-LICENSE.txt',
                  'resources/licenses/nanosic_wnvc_decoder.c', 'resources/licenses/nanosic-build.json',
                  'resources/lib/codebuddy_agent.py', 'resources/lib/codebuddy_account.py', 'resources/lib/codebuddy_protocol.py',
                  'resources/lib/codebuddy_regions.py',
                  'resources/lib/codebuddy_metadata.py',
                  'resources/lib/codebuddy_resident.py',
                  'resources/licenses/CODEBUDDY-LICENSE', 'resources/licenses/codebuddy-build.json',
                  'resources/licenses/CODEX-LICENSE', 'resources/licenses/CODEX-NOTICE', 'resources/licenses/codex-build.json',
                  'resources/lib/qrcodegen.py', 'resources/licenses/QRCODEGEN-LICENSE',
                  'resources/lib/codex_dictation_worker.py', 'resources/lib/codex_dictation.py',
                  'resources/lib/codex_dictation_recognizer.py', 'resources/lib/pcm_worker.py',
                  'resources/skins/Default/1080i/CodexLogin.xml',
                  'resources/lib/recording_ui.py', 'resources/skins/Default/1080i/VoiceRecording.xml',
                  'resources/skins/Default/media/codex-login-white.png',
                  'resources/licenses/cmcc-build.json',
                  'resources/vendor/websocket/__init__.py', 'resources/licenses/WEBSOCKET-CLIENT-LICENSE',
                  'resources/licenses/websocket-client-build.json',
                  'resources/lib/application.py', 'resources/lib/settings.py',
                  'resources/lib/io_wait.py',
                  'resources/lib/inputs/runtime.py', 'resources/lib/asr/factory.py',
                  'resources/lib/intelligence/runtime.py', 'resources/lib/configuration/server.py',
                  'resources/lib/intelligence/ui_context.py',
                  'resources/lib/intelligence/commands.py',
                  'resources/lib/intelligence/command_request.py',
                  'resources/lib/intelligence/command_result.py',
                  'resources/lib/intelligence/media_results.py',
                  'resources/lib/intelligence/media_resolver.py',
                  'resources/lib/intelligence/media_actions.py',
                  'resources/lib/intelligence/task.py',
                  'resources/lib/ui/task_requests.py',
                  'resources/lib/ui/task_playback.py',
                  'resources/lib/intelligence/juku.py', 'resources/lib/intelligence/juku_playback.py',
                  'resources/lib/intelligence/juku_tools.py',
                  'resources/lib/intelligence/emby.py', 'resources/lib/intelligence/emby_client.py',
                  'resources/lib/intelligence/emby_cache.py',
                  'resources/lib/intelligence/emby_queue.py',
                  'resources/lib/intelligence/emby_playback.py', 'resources/lib/intelligence/emby_tools.py',
                  'resources/lib/intelligence/emby_versions.py',
                  'resources/lib/intelligence/media_order.py',
                  'resources/lib/intelligence/command_rules.py',
                  'resources/lib/intelligence/command_resume.py',
                  'resources/lib/intelligence/command_media.py', 'resources/lib/intelligence/media_match.py',
                  'resources/lib/intelligence/media_catalog.py', 'resources/lib/intelligence/library_changes.py',
                  'resources/config/media-pronunciations.json',
                  'resources/licenses/PYPINYIN-LICENSE.txt', 'resources/licenses/media-pronunciations.json',
                  'resources/lib/asr/recognizer.py', 'resources/lib/asr/service.py', 'resources/lib/asr/native.py',
                  'resources/lib/dependencies/catalog.py', 'resources/lib/dependencies/download.py',
                  'resources/lib/dependencies/service.py', 'resources/lib/dependencies/ui.py',
                  'resources/web/index.html', 'resources/config/asr-test.wav',
                  'resources/config/restricted-model-template.json',
                  'resources/skins/Default/1080i/VoiceConversation.xml'):
        if not (ROOT / asset).is_file():
            raise RuntimeError('Missing {}. Run dev/build_speech.py and the remote decoder build scripts.'.format(asset))
    addon = ET.parse(ROOT / 'addon.xml').getroot()
    addon_id = addon.get('id')
    for asset in addon.findall("extension[@point='xbmc.addon.metadata']/assets/*"):
        if not (asset.text or '').strip() or not (ROOT / asset.text.strip()).is_file():
            raise RuntimeError('Missing metadata asset: {}'.format(asset.text))
    output = ROOT / 'dist' / '{}-{}.zip'.format(addon_id, addon.get('version'))
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
        for name in ('addon.xml', 'default.py', 'service.py', 'LICENSE.txt', 'README.md', 'command.md'):
            archive.write(ROOT / name, addon_id + '/' + name)
        for path in sorted((ROOT / 'resources').rglob('*')):
            if packaged(path):
                archive.write(path, addon_id + '/' + path.relative_to(ROOT).as_posix())
    return output


if __name__ == '__main__':
    print(build())
