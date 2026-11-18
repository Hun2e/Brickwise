"""Windows 네이티브 색인이 같은 저장소를 ASCII 경로로 열도록 한다."""

import ctypes
import hashlib
import os
from pathlib import Path
import tempfile


def persistence_path(path: Path) -> str:
    """데이터를 옮기지 않고 Windows의 동일 디렉터리 짧은 이름을 사용한다."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" or str(path).isascii():
        return str(path)
    # HNSW의 파일 열기는 Windows에서 UTF-8 한글 절대 경로를 처리하지 못한다.
    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    size = get_short_path(str(path), None, 0)
    if size:
        buffer = ctypes.create_unicode_buffer(size)
        written = get_short_path(str(path), buffer, size)
        if 0 < written < size and buffer.value.isascii():
            return buffer.value
    # 8.3 이름에도 한글이 남는 볼륨은 복사 대신 같은 폴더의 영문 junction을 쓴다.
    import _winapi

    bases = [parent for parent in path.parents if str(parent).isascii()]
    bases.append(Path(tempfile.gettempdir()))
    token = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    for base in bases:
        if not str(base).isascii():
            continue
        alias = base / ".chroma-paths" / token
        try:
            alias.parent.mkdir(parents=True, exist_ok=True)
            if not alias.exists():
                # CPython의 Windows API로 대괄호를 wildcard로 해석하지 않고 연결한다.
                try:
                    _winapi.CreateJunction(str(path), str(alias))
                except FileExistsError:
                    pass  # 다른 서버 프로세스가 먼저 만든 경우에도 아래에서 대상을 검증한다.
            if alias.is_dir() and alias.samefile(path):
                return str(alias)
        except OSError:
            continue
    raise ValueError("색인용 영문 경로를 만들 수 없습니다. 폴더 쓰기 권한을 확인하거나 "
                     "config/settings.yaml의 paths.chroma_dir을 영문 경로로 설정하세요.")
