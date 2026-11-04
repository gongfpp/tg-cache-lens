"""Create sparse temporary files for cached media with gaps."""
import os


def prepare_sparse(file):
    if os.name != 'nt':
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    device_control = ctypes.WinDLL('kernel32', use_last_error=True).DeviceIoControl
    device_control.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    device_control.restype = wintypes.BOOL
    returned = wintypes.DWORD()
    handle = msvcrt.get_osfhandle(file.fileno())
    if not device_control(handle, 0x000900C4, None, 0, None, 0, ctypes.byref(returned), None):
        raise OSError(ctypes.get_last_error(), '临时目录所在文件系统不支持稀疏文件')
