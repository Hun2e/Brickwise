"""설정 덮어쓰기, 안전한 CLI 오류, 공통 JSON·Markdown 결과."""

import argparse
import hashlib
import json
import re
import sys
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("이름은 영문·숫자로 시작하고 영문·숫자·밑줄·점·하이픈만 사용하세요.")
    return value


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--settings", default="config/settings.yaml")
    result.add_argument("--override", action="append", default=[], metavar="key=value")
    return result


def configured(args: argparse.Namespace) -> dict:
    return overrides(load_settings(args.settings), args.override)


def overrides(settings: dict, values: list[str]) -> dict:
    """존재하는 키에 타입이 같은 값만 적용한다. 원본은 바꾸지 않는다."""
    result = deepcopy(settings)
    for item in values:
        try:
            key, raw = item.split("=", 1)
            parts = key.split(".")
            parent = result
            for part in parts[:-1]:
                parent = parent[part]
            old, new = parent[parts[-1]], yaml.safe_load(raw)
            if type(old) is not type(new):
                raise ValueError
            parent[parts[-1]] = new
        except (KeyError, TypeError, ValueError, yaml.YAMLError):
            raise ValueError("override의 설정 키와 값 타입을 확인하세요.") from None
    cfg = result["retrieval"]
    from rag_service.rag.retrieve import mmr_caps
    mmr_caps(cfg)
    if cfg["search_type"] not in {"similarity", "mmr"}:
        raise ValueError("검색 방식은 similarity 또는 mmr이어야 합니다.")
    if cfg["query_mode"] not in {"expanded", "question", "dual"}:
        raise ValueError("질의 구성을 확인하세요.")
    if not 0 <= cfg["mmr_lambda"] <= 1 or type(cfg["mmr_apply_caps"]) is not bool:
        raise ValueError("MMR lambda와 상한 적용 설정을 확인하세요.")
    if any(cfg[key] <= 0 for key in ("similarity_fetch_k", "mmr_fetch_k")):
        raise ValueError("후보 수는 양수여야 합니다.")
    for key in ("top_k", "max_en_terms", "max_chunks_per_doc", "max_series_chunks",
                "max_group_chunks", "max_list_chunks"):
        if cfg[key] < 0:
            raise ValueError("검색 설정은 음수일 수 없습니다.")
    if cfg["fetch_multiplier"] <= 0:
        raise ValueError("검색 후보 배수는 양수여야 합니다.")
    return result


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                            for row in rows), encoding="utf-8")


def table(headers: list[str], rows: list[list]) -> str:
    def line(values: list) -> str:
        return "| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |"
    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def write_result(name: str, experiment: str, settings: dict, rows: list[dict],
                 summary: dict, markdown: str, **metadata: object) -> Path:
    now = datetime.now(timezone.utc)
    path = resolve_path(settings["evaluation"]["results_dir"]) / f"{now:%Y-%m-%d}_{safe_name(name)}.json"
    # 실험명 재사용은 기존 근거를 덮어쓰지 않는다.
    if path.exists() or path.with_suffix(".md").exists():
        raise ValueError("같은 날짜·실험명 결과가 있습니다. 새 --name을 지정하세요.")
    write_json(path, {"schema_version": 1, "experiment": experiment, "created_at": now.isoformat(),
                      "settings": settings, "settings_hash": digest(settings),
                      **metadata, "rows": rows, "summary": summary})
    path.with_suffix(".md").write_text(f"# {name}\n\n{markdown}\n", encoding="utf-8")
    return path


def guarded(run: Callable[[], int]) -> int:
    """예외 메시지의 키·프롬프트가 콘솔에 노출되지 않게 한다."""
    try:
        return run()
    except MissingApiKey as exc:
        _log_error(exc)
        print("API 키가 설정되지 않았습니다. 실행 환경에 GMS_KEY를 설정하세요.", file=sys.stderr)
    except Exception as exc:
        _log_error(exc)
        print("평가를 실행하지 못했습니다. 입력·중복 실험명·스냅샷·DB·설정과 연결 상태를 확인하세요.",
              file=sys.stderr)
    return 1


def _log_error(exc: Exception) -> None:
    """상세 예외는 로그 파일에만 쓰고 기록 실패는 콘솔로 전파하지 않는다."""
    try:
        path = resolve_path(load_settings()["evaluation"]["error_log"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{datetime.now(timezone.utc).isoformat()}\n")
            stream.write("".join(traceback.format_exception(exc)))
    except Exception:
        pass
