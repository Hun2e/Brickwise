"""한글·대괄호 경로의 디스크 HNSW를 별도 프로세스에서 다시 연다."""

import os
from pathlib import Path
import subprocess
import sys

from rag_service.vectorstore.paths import persistence_path


def test_path_keeps_same_directory(tmp_path):
    directory = tmp_path / "한글 [LEGO]" / "색인"
    result = persistence_path(directory)
    assert Path(result).samefile(directory)
    if os.name == "nt":
        assert result.isascii()


def test_persisted_hnsw_reopens_in_new_process(tmp_path):
    directory = tmp_path / "제출 [LEGO]" / "색인"
    script = '''
import sys
from pathlib import Path
from langchain_core.embeddings import DeterministicFakeEmbedding
from rag_service.vectorstore.store import get_collection
from rag_service.services.status import _index_counts
from rag_service.eval.snapshot import open_client
settings = {"paths": {"chroma_dir": sys.argv[1]},
            "collections": {"items": "test-meta", "docs": "test-docs"}}
store = get_collection("test-meta", DeterministicFakeEmbedding(size=8), settings)
if sys.argv[2] == "write":
    store.add_texts([f"record {i}" for i in range(1100)], ids=[str(i) for i in range(1100)])
else:
    assert _index_counts(settings) == (1100, 0)
    assert len(store.similarity_search("record 42", k=3)) == 3
    assert open_client(Path(sys.argv[1])).get_collection("test-meta").count() == 1100
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "PYTHON_DOTENV_DISABLED": "1", "ANONYMIZED_TELEMETRY": "False"}
    for mode in ("write", "read"):
        result = subprocess.run([sys.executable, "-c", script, str(directory), mode],
                                capture_output=True, text=True, env=env, timeout=60)
        assert result.returncode == 0, result.stderr
