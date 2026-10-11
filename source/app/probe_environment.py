"""Read-only Windows process identity, token and engine-module metadata.

Does not read/write process memory, inject code, load game DLLs, alter tokens,
enable privileges, install a bridge, or enqueue IPC requests.
Writes JSON only when --out is explicitly selected; otherwise stdout only.
"""
import argparse
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    def token_info(process):
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return {'error': ctypes.get_last_error()}
        try:
            result = {}
            for key, klass in [('elevated', 20), ('elevation_type', 18)]:
                value, count = wintypes.DWORD(), wintypes.DWORD()
                ok = advapi.GetTokenInformation(token, klass, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(count))
                result[key] = (bool(value.value) if key == 'elevated' else value.value) if ok else {'error': ctypes.get_last_error()}
            return result
        finally:
            kernel.CloseHandle(token)

    now = dt.datetime.now().astimezone()
    result = {'observed_at': now.isoformat(), 'target_pid': args.pid,
              'observer_token': token_info(kernel.GetCurrentProcess()),
              'access_checks': [],
              'limitations': ['Handle-open tests do not read game memory or establish a supported mod API.',
                              'A denied access check alone does not identify its cause.']}
    for name, access in [('query_limited', 0x1000)]:
        ctypes.set_last_error(0)
        handle = kernel.OpenProcess(access, False, args.pid)
        check = {'mode': name, 'access_mask': hex(access), 'handle_ok': bool(handle),
                 'winerror': 0 if handle else ctypes.get_last_error()}
        if handle:
            try:
                if name == 'query_limited':
                    buf, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                        result['target_image'] = buf.value
                    else:
                        result['target_image_error'] = ctypes.get_last_error()
                    result['target_token'] = token_info(handle)
                    times = [wintypes.FILETIME() for _ in range(4)]
                    if kernel.GetProcessTimes(handle,*[ctypes.byref(stamp) for stamp in times]):
                        result['target_created_unix'] = ((times[0].dwHighDateTime<<32)+times[0].dwLowDateTime)/10000000-11644473600
                    else:
                        result['target_created_error'] = ctypes.get_last_error()
            finally:
                kernel.CloseHandle(handle)
        result['access_checks'].append(check)
    # Standard Windows module metadata enumeration; never dereference addresses.
    class ModuleEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD),
                    ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.c_void_p),
                    ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE),
                    ('szModule', wintypes.WCHAR * 256), ('szExePath', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
    kernel.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
    result['engine_modules'] = []
    if result.get('target_image') and Path(result['target_image']).name.lower() == 'dwrg.exe':
        snap = kernel.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, args.pid)
        if snap == ctypes.c_void_p(-1).value:
            result['module_snapshot_error'] = ctypes.get_last_error()
        else:
            try:
                entry = ModuleEntry()
                entry.dwSize = ctypes.sizeof(entry)
                ok = kernel.Module32FirstW(snap, ctypes.byref(entry))
                if not ok:
                    result['module_first_error'] = ctypes.get_last_error()
                while ok:
                    if entry.szModule.lower() in ('dwrg.exe', 'neox_engine.dll', 'gameassembly.dll', 'unityplayer.dll'):
                        module_path = Path(entry.szExePath)
                        item = {'name': entry.szModule, 'path': str(module_path),
                                'image_size': entry.modBaseSize}
                        try:
                            item['disk_sha256'] = sha256_file(module_path)
                        except OSError as exc:
                            item['disk_read_error'] = str(exc)
                        result['engine_modules'].append(item)
                    ok = kernel.Module32NextW(snap, ctypes.byref(entry))
            finally:
                kernel.CloseHandle(snap)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
