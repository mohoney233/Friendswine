"""Portable paths/config and read-only process identity helpers."""
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path

VERSION = '1.3.0'
ENGINE_SHA = 'fbc8e87c97bb5b5b7e9d3d5de3668c04255ce9afd6038bdf29fa1f7de4ae8af6'


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def installation_key(app_root):
    canonical = os.path.normcase(str(Path(app_root).resolve()))
    return hashlib.sha256(canonical.encode('utf8')).hexdigest()[:16]


def state_directory(app_root, data_home=None):
    # The installation is self-contained: user state lives in a 'data' directory
    # next to the app, and that location is a pure function of the app path. It
    # is therefore identical for every account that runs this copy, so nothing
    # is ever written to a per-user profile on the system drive. An explicit
    # override stays available for callers that need to relocate it.
    if data_home is not None:
        if isinstance(data_home,str) and not data_home.strip():
            raise ValueError('explicit data_home must be an absolute path')
        base = Path(data_home)
    else:
        base = Path(app_root).resolve().parent/'data'
    if not base.is_absolute():
        raise ValueError('data_home must be an absolute path')
    return base/'Friendswine'/installation_key(app_root)


def normalized_path(path):
    return os.path.normcase(str(Path(path).resolve()))


def validate_game_path(raw):
    if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
        raise ValueError('请提供 dwrg.exe 的绝对路径')
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError('游戏路径必须是绝对路径')
    path = path.resolve(strict=True)
    if not path.is_file() or path.name.lower() != 'dwrg.exe':
        raise ValueError('游戏路径必须指向存在的 dwrg.exe 文件')
    if not (path.parent/'neox_engine.dll').is_file():
        raise ValueError('游戏目录缺少 neox_engine.dll')
    return path


def load_configuration(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('配置文件无法读取；请重新 configure --game') from error
    if type(data) is not dict or data.get('schema') != 1:
        raise ValueError('配置格式不受支持；请重新 configure --game')
    if not isinstance(data.get('game_exe'), str):
        raise ValueError('配置缺少 game_exe')
    return data


def process_image(pid):
    """Query the actual executable image; no memory read or token mutation."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def select_game(config, pids, image_reader=process_image):
    if len(pids) != 1:
        raise RuntimeError('需要恰好一个 dwrg.exe 进程；当前 '+repr(pids))
    image = validate_game_path(str(image_reader(pids[0])))
    configured = config.get('game_exe')
    if configured is not None:
        requested = validate_game_path(configured)
        if normalized_path(requested) != normalized_path(image):
            raise RuntimeError('配置游戏路径与运行进程的真实 image 不一致')
    return image
