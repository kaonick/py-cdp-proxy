import os
import subprocess
import signal
import tempfile
import psutil

def check_pid_alive(pid):
    if psutil.pid_exists(pid):
        return True
    else:
        return False
def kill_pids(pids:list):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM) #or SIGINT, SIGABRT, SIGTERM, SIGSEGV, SIGILL, and SIGFPE.
        except Exception as err:
            print(err)