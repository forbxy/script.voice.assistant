"""Route a voice invocation to the existing conversation without starting capture."""
import time

PREFIX = 'VoiceAssistant.'


def request_existing(home, source):
    if home.getProperty(PREFIX + 'State') not in ('thinking', 'conversation'):
        return
    if home.getProperty(PREFIX + 'Background') == 'true':
        home.setProperty(PREFIX + 'ForegroundRequested', str(time.monotonic()))
    elif source == 'usb':
        home.setProperty(PREFIX + 'USBRecordRequested', str(time.monotonic()))
