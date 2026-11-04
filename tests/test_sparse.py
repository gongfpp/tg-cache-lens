import os
import pytest
from tg_cache_lens.sparse import prepare_sparse


@pytest.mark.skipif(os.name != 'nt', reason='Windows allocation check')
def test_windows_sparse_snapshot_does_not_allocate_gap(tmp_path):
    import ctypes
    from ctypes import wintypes
    path = tmp_path/'sparse'
    with path.open('wb') as output:
        prepare_sparse(output)
        output.seek(64 * 1024 * 1024)
        output.write(b'x')
    query = ctypes.WinDLL('kernel32', use_last_error=True).GetCompressedFileSizeW
    query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.DWORD
    high = wintypes.DWORD()
    low = query(str(path), ctypes.byref(high))
    allocated = (high.value << 32) | low
    assert path.stat().st_size == 64 * 1024 * 1024 + 1
    assert allocated < 1024 * 1024
