"""섹션 경계를 우선하는 결정적 본문 청킹."""

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag_service.data_prep.collectors.base import Doc


@dataclass(frozen=True)
class Chunk:
    seq: int
    text: str
    section_path: str
    char_len: int


def chunk_doc(doc: Doc, chunk_size: int, overlap: int, min_chars: int,
              header: bool) -> list[Chunk]:
    """본문 길이만 제한하고, 같은 부모의 인접한 짧은 조각을 병합한다."""
    if chunk_size <= 0 or not 0 <= overlap < chunk_size or not 0 <= min_chars <= chunk_size:
        raise ValueError("청크 크기·중복·최소 길이 설정을 확인하세요.")
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    stack: list[tuple[int, str, int]] = []
    pieces: list[tuple[tuple[int, ...], list[str], str]] = []
    for index, section in enumerate(doc.sections):
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        parent = tuple(entry[2] for entry in stack)
        if section.heading:
            stack.append((section.level, section.heading, index))
        path = " › ".join(entry[1] for entry in stack)
        body = section.text.strip()
        if not body:
            continue
        texts = splitter.split_text(body) if len(body) > chunk_size else [body]
        for text in texts:
            if pieces:
                previous_parent, paths, previous = pieces[-1]
                merged = previous + "\n\n" + text
                if (parent == previous_parent and len(merged) <= chunk_size
                        and min(len(previous), len(text)) < min_chars):
                    pieces[-1] = (parent, list(dict.fromkeys([*paths, path])), merged)
                    continue
            pieces.append((parent, [path], text))
    result = []
    for seq, (_, paths, body) in enumerate(pieces):
        path = " | ".join(path for path in paths if path)
        heading = f"[{doc.title}{' › ' + path if path else ''}]\n"
        result.append(Chunk(seq, heading + body if header else body, path, len(body)))
    return result
