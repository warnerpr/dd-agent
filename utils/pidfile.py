import logging
import os.path
import tempfile

from utils.platform import Platform

log = logging.getLogger(__name__)


class PidFile(object):
    """ A small helper class for pidfiles. """

    @classmethod
    def get_dir(cls):
        my_dir = os.path.dirname(os.path.abspath(__file__))
        if Platform.is_mac():
            run_dir = os.path.join(my_dir, '..', 'run')
        else:
            run_dir = os.path.join(my_dir, '..', '..', 'run')

        if os.path.exists(run_dir) and os.access(run_dir, os.W_OK):
            return run_dir
        else:
            return tempfile.gettempdir()

    def __init__(self, program, pid_dir=None):
        self.pid_file = "%s.pid" % program
        self.pid_dir = pid_dir or self.get_dir()
        self.pid_path = os.path.join(self.pid_dir, self.pid_file)

    def get_path(self):
        # if all else fails
        if os.access(self.pid_path, os.W_OK):
            log.info("Pid file is: %s" % self.pid_path)
            return self.pid_path
        else:
            # Can't save pid file, bail out
            log.error("Cannot save pid file anywhere")
            raise Exception("Cannot save pid file anywhere")

    def clean(self):
        try:
            path = self.get_path()
            log.debug("Cleaning up pid file %s" % path)
            os.remove(path)
            return True
        except Exception:
            log.warn("Could not clean up pid file")
            return False

    def get_pid(self):
        "Retrieve the actual pid"
        try:
            pf = open(self.get_path())
            pid_s = pf.read()
            pf.close()

            return int(pid_s.strip())
        except Exception:
            return None
