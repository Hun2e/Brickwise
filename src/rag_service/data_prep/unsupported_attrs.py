"""수집 범위 밖 제품 속성을 설정 사전으로 판별한다."""

import yaml

from rag_service.config import load_settings, resolve_path


def unsupported_answer(question: str, settings: dict | None = None) -> str | None:
    """미지원 속성은 외부 사실을 추측하지 않고 미수집 이유를 안내한다."""
    settings = settings or load_settings()
    with resolve_path(settings["paths"]["unsupported_attrs"]).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    folded = question.casefold()
    reasons = [row["reason"] for row in config["attributes"]
               if any(word.casefold() in folded for word in row["keywords"])]
    if not reasons:
        return None
    return "이 정보는 수집하지 않아 확인되지 않습니다. " + " ".join(reasons) + "\n" + config["alternative"]
