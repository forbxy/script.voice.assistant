"""Deploy, verify native offline recognition, register the add-on."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import time

import paramiko

ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = 'script.voice.assistant'
REMOTE = '/storage/.kodi/addons/' + ADDON_ID


def installation_script(stage, target=REMOTE, addon_id=ADDON_ID):
    """Keep mapped shared libraries intact until Kodi releases their inode."""
    return '''import hashlib,os,shutil,tempfile
from pathlib import Path
source=Path(%r)/%r
target=Path(%r)
changed=0
for path in source.rglob('*'):
 if not path.is_file(): continue
 destination=target/path.relative_to(source)
 if destination.is_file() and hashlib.sha256(destination.read_bytes()).digest()==hashlib.sha256(path.read_bytes()).digest(): continue
 destination.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as output:
  temporary=Path(output.name)
 try:
  shutil.copy2(path,temporary)
  os.replace(temporary,destination)
 finally:
  temporary.unlink(missing_ok=True)
 changed+=1
shutil.rmtree(%r)
print('Atomically updated',changed,'files')
''' % (stage, addon_id, target, stage)


def configuration_script(settings, status):
    """Save settings even when Kodi forwards setSetting to an open dialog."""
    return '''import json
from pathlib import Path
import xbmc,xbmcaddon
requested=%r
try:
 addon=xbmcaddon.Addon(%r)
 monitor=xbmc.Monitor()
 for name,value in requested.items(): addon.setSetting(name,value)
 actual={name:addon.getSetting(name) for name in requested}
 if actual!=requested and xbmc.getCondVisibility('Window.IsActive(addonsettings)'):
  # setSetting queues edits into this addon's open settings dialog instead
  # of writing its profile. Save pending edits, then apply the new settings.
  monitor.waitForAbort(0.2)
  xbmc.executebuiltin('SendClick(10140,28)',True)
  monitor.waitForAbort(0.2)
  for name,value in requested.items(): addon.setSetting(name,value)
 actual={name:addon.getSetting(name) for name in requested}
 if actual!=requested: raise RuntimeError('Kodi did not save requested settings')
 result={'ok':True,'settings':actual}
except Exception as error:
 result={'ok':False,'error':str(error)}
Path(%r).write_text(json.dumps(result))
''' % (settings, ADDON_ID, status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', help='16 kHz mono PCM WAV; enables native recognition and USB device check')
    parser.add_argument('--api-key-file', help='Import a private API key into the CE profile and enable the AI assistant')
    parser.add_argument('--doubao-key-file', help='Import a private Doubao ASR Key; retain the selected recognition engine')
    parser.add_argument('--voice-input', choices=('auto', 'cmcc', 'nanosic', 'ur02', 'usb'), help='Select the primary voice input source')
    parser.add_argument('--cmcc-activation', choices=('tap', 'hold'), help='Choose tap or hold to speak')
    parser.add_argument('--agent-backend', choices=('builtin', 'codex'), help='Choose built-in Agent or full-permission Codex')
    parser.add_argument('--recognition-engine', choices=('ncnn', 'vosk', 'doubao', 'codex'), help='Select the speech recognition engine')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('voice_build', ROOT / 'build_package.py')
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    package = build.build()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(os.environ.get('VOICE_CE_HOST', '192.168.50.112'),
                   username=os.environ.get('VOICE_CE_USER', 'root'),
                   password=os.environ.get('VOICE_CE_PASSWORD', 'coreelec'), timeout=10)

    def execute(command, timeout=60):
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        output, error = stdout.read().decode(), stderr.read().decode()
        if stdout.channel.recv_exit_status():
            raise RuntimeError(error or output)
        return output

    def rpc(method, params):
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
        script = ('import urllib.request; print(urllib.request.urlopen(urllib.request.Request('
                  "'http://127.0.0.1:8080/jsonrpc', data=" + repr(request) +
                  ", headers={'Content-Type':'application/json'}), timeout=15).read().decode())")
        response = json.loads(execute('python3 -c ' + shlex.quote(script)))
        if response.get('error'):
            raise RuntimeError(str(response['error']))
        return response['result']

    try:
        # Keep a rollback ZIP outside addons; existing profile/settings are preserved.
        execute('mkdir -p /storage/.kodi/addons/packages /storage/.cache/voice-assistant')
        backup = ('set -o pipefail; if [ -d ' + shlex.quote(REMOTE) + ' ]; then '
                  '(cd /storage/.kodi/addons && tar cf - ' + ADDON_ID + ') | gzip -1 > '
                  '/storage/.cache/voice-assistant/addon-before-deploy.tgz.tmp && mv '
                  '/storage/.cache/voice-assistant/addon-before-deploy.tgz.tmp '
                  '/storage/.cache/voice-assistant/addon-before-deploy.tgz; fi')
        execute(backup, timeout=180)
        remote_zip = '/storage/.kodi/addons/packages/' + package.name
        with client.open_sftp() as sftp:
            sftp.put(str(package), remote_zip)
            if args.api_key_file:
                from resources.lib.agent_config import read_key_file
                key = read_key_file(args.api_key_file)
                remote_profile = '/storage/.kodi/userdata/addon_data/' + ADDON_ID
                execute('mkdir -p ' + shlex.quote(remote_profile))
                temporary = remote_profile + '/agent_credentials.json.tmp'
                with sftp.open(temporary, 'w') as output:
                    output.write(json.dumps({'api_key': key}))
                sftp.chmod(temporary, 0o600)
                sftp.posix_rename(temporary, remote_profile + '/agent_credentials.json')
            if args.doubao_key_file:
                from resources.lib.doubao_config import read_key_file as read_doubao_key
                key = read_doubao_key(args.doubao_key_file)
                remote_profile = '/storage/.kodi/userdata/addon_data/' + ADDON_ID
                execute('mkdir -p ' + shlex.quote(remote_profile))
                temporary = remote_profile + '/doubao_credentials.json.tmp'
                with sftp.open(temporary, 'w') as output:
                    output.write(json.dumps({'api_key': key}))
                sftp.chmod(temporary, 0o600)
                sftp.posix_rename(temporary, remote_profile + '/doubao_credentials.json')
            if args.fixture:
                sftp.put(str(Path(args.fixture).resolve()), '/storage/.cache/voice-assistant/fixture.wav')
                sftp.put(str(ROOT / 'dev/device_check.py'), '/storage/.cache/voice-assistant/device_check.py')
        stage = '/storage/.cache/voice-assistant/deploy-stage'
        execute('mkdir -p ' + shlex.quote(stage) + ' && unzip -o ' + shlex.quote(remote_zip) +
                ' -d ' + shlex.quote(stage) + ' >/dev/null', timeout=180)
        # Truncating a mapped .so discards relocated private pages and can
        # crash the next dlopen even when the replacement bytes are identical.
        # Keep unchanged inodes, and atomically replace changed files so old
        # mappings continue using their original inode.
        script = installation_script(stage)
        print(execute('python3 -c ' + shlex.quote(script), timeout=180), flush=True)
        execute('python3 -m compileall -q ' + shlex.quote(REMOTE))
        if args.fixture:
            report = json.loads(execute('python3 /storage/.cache/voice-assistant/device_check.py /storage/.cache/voice-assistant/fixture.wav', 120))
            (ROOT / 'dev/device_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        execute('kodi-send --action="UpdateLocalAddons"')
        for attempt in range(20):
            try:
                details = rpc('Addons.GetAddonDetails', {'addonid': ADDON_ID, 'properties': ['enabled', 'version']})
                break
            except RuntimeError:
                time.sleep(0.5)
        else:
            raise RuntimeError('Kodi did not discover ' + ADDON_ID)
        rpc('Addons.SetAddonEnabled', {'addonid': ADDON_ID, 'enabled': True})
        details = rpc('Addons.GetAddonDetails', {'addonid': ADDON_ID, 'properties': ['enabled', 'version']})
        if args.voice_input or args.cmcc_activation or args.agent_backend or args.api_key_file or args.recognition_engine:
            source_path = '/storage/.cache/voice-assistant/configure_voice_input.py'
            status_path = '/storage/.cache/voice-assistant/configure_voice_input.json'
            requested = {name: value for name, value in (
                ('voice_input', args.voice_input), ('cmcc_activation', args.cmcc_activation),
                ('agent_backend', args.agent_backend), ('recognition_engine', args.recognition_engine),
                ('agent_enabled', 'true' if args.api_key_file else None)) if value}
            source_script = configuration_script(requested, status_path)
            with client.open_sftp() as sftp:
                try:
                    sftp.remove(status_path)
                except FileNotFoundError:
                    pass
                with sftp.open(source_path, 'w') as output:
                    output.write(source_script)
            execute('kodi-send --action=' + shlex.quote('RunScript(' + source_path + ')'))
            for attempt in range(40):
                with client.open_sftp() as sftp:
                    try:
                        with sftp.open(status_path) as source:
                            configured = json.loads(source.read())
                    except FileNotFoundError:
                        configured = None
                if configured is not None:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError('Kodi setting update timed out')
            if not configured['ok']:
                raise RuntimeError(configured['error'])
            print(json.dumps(configured, ensure_ascii=False), flush=True)
        label = 'Window(10000).Property(VoiceAssistant.CMCCService)'
        if rpc('XBMC.GetInfoLabels', {'labels': [label]}).get(label) == 'running':
            # Reload this service alone so its already-imported Python modules
            # use the new implementation; the Kodi process remains running.
            version_label = 'Window(10000).Property(VoiceAssistant.CMCCServiceVersion)'
            if rpc('XBMC.GetInfoLabels', {'labels': [version_label]}).get(version_label):
                # Ask this service to return normally. StopScript can inject
                # SystemExit during imports and force-kill the interpreter.
                execute('touch /storage/.kodi/userdata/addon_data/' + ADDON_ID + '/cmcc-stop.flag')
            else:
                execute('kodi-send --action=' + shlex.quote('StopScript(' + REMOTE + '/service.py)'))
            for attempt in range(20):
                if rpc('XBMC.GetInfoLabels', {'labels': [label]}).get(label) != 'running':
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('Previous CMCC service did not stop; Kodi was not restarted.')
        # An already-running Kodi may not auto-start a newly added service
        # extension. Start it once; cmcc-service.lock deduplicates auto/manual starts.
        execute('kodi-send --action=' + shlex.quote('RunScript(' + REMOTE + '/service.py)'))
        for attempt in range(20):
            if rpc('XBMC.GetInfoLabels', {'labels': [label]}).get(label) == 'running':
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('CMCC Bluetooth service did not start; inspect Kodi log.')
        if args.api_key_file:
            print('API Key 已保存到私有资料目录，AI 助手已启用。', flush=True)
        if args.doubao_key_file:
            print('豆包 Key 已保存到私有资料目录，识别引擎选择保持原设置。', flush=True)
        print(json.dumps(details, ensure_ascii=False), flush=True)
        print('部署完成。' + ('中文测试语音识别已通过；遥控器口述待实测。' if args.fixture else ''), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
