"""Власне дерево дочірніх процесів: жодних живих нащадків після виходу."""

import os
import signal
import subprocess
import time


def _validate_memory_limit(memory_limit):
    if memory_limit is None:
        return
    import ctypes
    maximum = (1 << (ctypes.sizeof(ctypes.c_size_t) * 8)) - 1
    if type(memory_limit) is not int or not 0 < memory_limit <= maximum:
        raise ValueError("Ліміт пам'яті має бути додатним цілим числом байтів у межах SIZE_T.")


class _WindowsJob:
    """CREATE_SUSPENDED -> Job -> ResumeThread: нащадки не встигають утекти."""

    def __init__(self, *, memory_limit=None):
        import ctypes
        from ctypes import wintypes

        _validate_memory_limit(memory_limit)
        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        size_t = ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", size_t), ("MaximumWorkingSetSize", size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", size_t), ("JobMemoryLimit", size_t),
                        ("PeakProcessMemoryUsed", size_t), ("PeakJobMemoryUsed", size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        self.Accounting, self.ThreadEntry = Accounting, ThreadEntry
        prototypes = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        }
        for name, (arguments, result) in prototypes.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            # Shared across the whole owned tree, including future descendants.
            # Virtual address reservation alone does not consume this budget.
            limits.BasicLimitInformation.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = memory_limit
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise error

    def attach(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def resume(self, process):
        ctypes = self.ctypes
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = []
            more = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == process.pid:
                    found.append(entry.th32ThreadID)
                more = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if len(found) != 1:
                raise OSError("Не вдалося визначити призупинений початковий потік процесу.")
            thread = self.api.OpenThread(0x2, False, found[0])  # THREAD_SUSPEND_RESUME
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.api.ResumeThread(thread) != 1:
                    raise OSError("Не вдалося безпечно запустити призупинений процес.")
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(snapshot)

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def wait_empty(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            info = self.Accounting()
            if not self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(info),
                                                      self.ctypes.sizeof(info), None):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if not info.ActiveProcesses:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Дерево дочірніх процесів не завершилося вчасно.")
            time.sleep(0.01)

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())


class ProcessTree:
    """Використання: Popen(..., **tree.popen_options), потім tree.attach(process).

    Контекст завершує також нащадків уже завершеного батька. Job закривається
    при аварії програми, а процес до attach() не виконує свого коду у Windows.
    """

    def __init__(self, *, timeout=10, memory_limit=None):
        _validate_memory_limit(memory_limit)
        if memory_limit is not None and os.name != "nt":
            raise RuntimeError("Обмеження пам'яті дерева процесів підтримується лише у Windows.")
        self.timeout = timeout
        self.process = None
        self.assigned = False
        self.closed_cleanly = False
        self._closed = False
        self.job = None
        if os.name == "nt":
            self.job = _WindowsJob(memory_limit=memory_limit) if memory_limit is not None else _WindowsJob()
        self.popen_options = ({"creationflags": subprocess.CREATE_NO_WINDOW | 0x4}
                              if self.job else {"start_new_session": True})

    def __enter__(self):
        return self

    def attach(self, process):
        self.process = process
        if self.job:
            self.job.attach(process)
            self.assigned = True
            self.job.resume(process)

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = self.process
        try:
            if process is not None:
                if self.job and self.assigned:
                    self.job.terminate()
                elif self.job:
                    # AssignProcessToJobObject відхилено: дитина ще suspended.
                    if process.poll() is None:
                        process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=self.timeout)
                if self.job and self.assigned:
                    self.job.wait_empty(self.timeout)
        finally:
            self.process = None
            self.assigned = False
            try:
                # Popen зберігає Windows HANDLE до GC; навіть завершений process
                # object може ще утримувати його робочий каталог. returncode уже
                # записано wait(), тому подальші poll()/wait() не потребують HANDLE.
                if self.job and process is not None and process.returncode is not None:
                    handle = getattr(process, "_handle", None)
                    if handle is not None:
                        handle.Close()
            finally:
                if self.job:
                    self.job.close()
        self.closed_cleanly = True

    def __exit__(self, kind, primary, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if primary is None:
                raise
            primary.add_note("Помилка завершення дерева процесів: " + str(cleanup))
        return False
