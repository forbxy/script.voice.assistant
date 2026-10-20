"""Wait for sockets, pipes and HID devices without Linux select's FD limit."""
import errno
import math
import operator
import select


def wait(readers, writers, exceptional, timeout=None):
    """Keep select's object lists and EOF semantics, using poll on Linux."""
    if not hasattr(select, 'poll'):
        return select.select(readers, writers, exceptional, timeout)
    if timeout is not None and timeout < 0:
        raise ValueError('timeout must be non-negative')
    milliseconds = None if timeout is None else math.ceil(timeout * 1000)
    groups = [list(readers), list(writers), list(exceptional)]
    descriptors, interests = [], {}
    for group, flags in zip(groups, (select.POLLIN, select.POLLOUT, select.POLLPRI)):
        values = []
        for value in group:
            fd = operator.index(value if isinstance(value, int) else value.fileno())
            if fd < 0:
                raise ValueError('file descriptor must be non-negative')
            values.append(fd)
            interests[fd] = interests.get(fd, 0) | flags
        descriptors.append(values)
    poller = select.poll()
    for fd, flags in interests.items():
        poller.register(fd, flags)
    events = dict(poller.poll(milliseconds))
    if any(flags & select.POLLNVAL for flags in events.values()):
        raise OSError(errno.EBADF, 'Bad file descriptor')
    # A hung-up reader must wake so recv/read can observe EOF. Writers also
    # wake on hangup/error so their existing broken-pipe handling can run.
    masks = (select.POLLIN | select.POLLHUP | select.POLLERR,
             select.POLLOUT | select.POLLHUP | select.POLLERR, select.POLLPRI)
    return tuple([value for value, fd in zip(group, fds) if events.get(fd, 0) & mask]
                 for group, fds, mask in zip(groups, descriptors, masks))
