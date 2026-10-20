from resources.lib.nanosic_frames import NanosicFrames

START = b'\x2bnanosic voice start\0'
STOP = b'\x2bnanosic voice stop \0'
KEY = bytes.fromhex('0100003e0000000000')


def packets(sequence, body):
    padded = body + bytes(6)
    result = [b'\x2b\xaa\x99' + bytes((sequence, 0, 60)) + padded[:15]]
    for number in range(1, 4):
        offset = 15 + (number - 1) * 17
        result.append(b'\x2b\x99' + bytes((sequence, number)) + padded[offset:offset + 17])
    return result


def collect(assembler, reports):
    return [event[1] for raw in reports if (event := assembler.feed(raw))
            and event[0] == 'frame']


def test_voice_boundaries_and_sequence_wrap_do_not_interpret_keys_as_audio():
    assembler = NanosicFrames()
    body = bytes(range(60))
    assert collect(assembler, packets(254, body)) == []
    assert assembler.feed(START) == ('start', None)
    sequence = packets(255, body)
    assert collect(assembler, sequence[:2] + [KEY] + sequence[2:]) == [body]
    assert collect(assembler, packets(0, body[::-1])) == [body[::-1]]
    assert assembler.stats['missing_frames'] == 0
    assert assembler.feed(STOP) == ('stop', None)
    assert collect(assembler, packets(1, body)) == []
    assert assembler.stats['frames'] == 2


def test_lost_fragment_is_discarded_and_next_frame_recovers():
    assembler = NanosicFrames()
    assembler.feed(START)
    body = bytes(range(60))
    assert collect(assembler, packets(10, body)) == [body]
    broken = packets(11, body)
    assert collect(assembler, [broken[0], broken[2], broken[3], broken[1]]) == []
    assert collect(assembler, packets(12, body)) == [body]
    assert assembler.stats['discarded_frames'] == 1
    assert assembler.stats['missing_frames'] == 1


def test_stop_and_restart_never_join_fragments_from_different_sessions():
    assembler = NanosicFrames()
    assembler.feed(START)
    body = bytes(range(60))
    fragment = packets(7, body)
    assert collect(assembler, fragment[:2]) == []
    assembler.feed(STOP)
    assembler.feed(START)
    assert collect(assembler, fragment[2:]) == []
    assert collect(assembler, packets(8, body)) == [body]
    assert assembler.stats['discarded_frames'] == 1
    assert assembler.stats['missing_frames'] == 0


def test_mismatched_sequence_bad_size_and_other_codecs_are_rejected():
    assembler = NanosicFrames()
    assembler.feed(START)
    body = bytes(range(60))
    first = packets(4, body)
    second = packets(5, body)
    assert collect(assembler, [first[0], second[1], first[2], first[3]]) == []
    assert collect(assembler, [first[0][:-1], first[0] + b'\0']) == []
    wrong_length = first[0][:5] + bytes((40,)) + first[0][6:]
    assert collect(assembler, [wrong_length] + first[1:]) == []
    other_codec = first[0][:2] + b'\x66' + first[0][3:]
    assert collect(assembler, [other_codec] + first[1:]) == []
    assert collect(assembler, second) == [body]
    assert assembler.stats['frames'] == 1
