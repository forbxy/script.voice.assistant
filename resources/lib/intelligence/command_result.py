"""One local command result envelope shared by rules and media actions."""
import json


def outcome(text, message, choices=(), trace=()):
    reply = json.dumps({'message': message, 'interaction': 'continue', 'choices': list(choices)}, ensure_ascii=False)
    return {'reply': reply, 'turn': [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}],
            'tools': list(trace), 'agent_backend': 'local', 'model_invocations': 0}
