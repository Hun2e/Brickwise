"""MediaWiki API를 사용하는 영·한 위키백과 수집기."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

from .base import Candidate, Doc
from .cleaning import clean_sections, split_sections
from .http import HttpClient, UpstreamError


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_html(value: str) -> str:
    parser = _PlainHTML()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


class WikipediaCollector:
    source = "wikipedia"

    def __init__(self, client: HttpClient, settings: dict):
        self.client = client
        self.settings = settings["wikipedia"]

    def _query(self, lang: str, **params) -> dict:
        if lang not in self.settings["langs"]:
            raise ValueError("지원하지 않는 위키백과 언어입니다.")
        return self.client.get_json(self.settings["api_url"].format(lang=lang), {
            "action": "query", "format": "json", "formatversion": 2, "redirects": 1,
            **params,
        })

    def _url(self, lang: str, title: str) -> str:
        return self.settings["article_url"].format(lang=lang, title=quote(title.replace(" ", "_"), safe=""))

    def _page(self, lang: str, title_or_id: str | int, **params) -> dict | None:
        selector = {"pageids": title_or_id} if isinstance(title_or_id, int) else {"titles": title_or_id}
        data = self._query(lang, **selector, **params)
        pages = data.get("query", {}).get("pages", [])
        if not pages:
            raise UpstreamError("위키백과 문서 정보가 없습니다. 잠시 후 다시 시도하세요.")
        page = pages[0]
        return None if "missing" in page or "invalid" in page else page

    def search(self, q: str, lang: str, limit: int) -> list[Candidate]:
        """검색 결과의 HTML을 제거하고 원문 URL을 제공한다."""
        if limit <= 0:
            raise ValueError("검색 결과 수는 양수여야 합니다.")
        data = self._query(lang, list="search", srsearch=q, srlimit=limit)
        if "search" not in data.get("query", {}):
            raise UpstreamError("위키백과 검색 응답이 올바르지 않습니다. 잠시 후 다시 시도하세요.")
        return [Candidate(self.source, lang, row["pageid"], row["title"],
                          _plain_html(row.get("snippet", "")), self._url(lang, row["title"]))
                for row in data["query"]["search"][:limit]]

    def latest_revid(self, lang: str, title_or_id: str | int) -> tuple[int, int, str] | None:
        """본문 없이 최종 문서 ID와 revision을 조회한다."""
        page = self._page(lang, title_or_id, prop="info|revisions", rvprop="ids")
        if page is None:
            return None
        return page["pageid"], page["revisions"][0]["revid"], page["title"]

    def fetch(self, lang: str, title_or_id: str | int, search_query: str | None = None) -> Doc | None:
        """평문 본문을 정제하고 출처·revision·통계를 보존한다."""
        page = self._page(lang, title_or_id, prop="extracts|info|revisions", explaintext=1,
                          exsectionformat="wiki", rvprop="ids", inprop="url")
        if page is None:
            return None
        if "extract" not in page:
            raise UpstreamError("위키백과 본문이 없습니다. 잠시 후 다시 시도하세요.")
        raw = split_sections(page["extract"])
        sections = clean_sections(raw, self.settings["drop_sections"][lang], self.settings["min_section_chars"])
        url = page.get("fullurl") or self._url(lang, page["title"])
        doc = Doc(
            doc_id=f"wp-{lang}:{page['pageid']}", source=self.source, lang=lang,
            page_id=page["pageid"], title=page["title"], url=url,
            revid=page["revisions"][0]["revid"], license="CC BY-SA 4.0",
            attribution=f"Wikipedia contributors, {page['title']}, {url}", sections=sections,
            fetched_at=datetime.now(timezone.utc).isoformat(), search_query=search_query,
        )
        doc.cleaning_stats = {"before_chars": len(page["extract"]), "after_chars": len(doc.text()),
                              "kept_sections": len(sections), "dropped_sections": len(raw) - len(sections)}
        return doc

    def langlinks(self, titles: list[str], from_lang: str, to_lang: str) -> dict[str, str | None]:
        """정규화와 리다이렉트 뒤에도 요청한 제목을 키로 유지한다."""
        if to_lang not in self.settings["langs"]:
            raise ValueError("지원하지 않는 위키백과 언어입니다.")
        if not titles:
            return {}
        # MediaWiki의 일반 클라이언트 제목 제한은 요청당 50개다.
        if len(titles) > 50:
            result = {}
            for start in range(0, len(titles), 50):
                result.update(self.langlinks(titles[start:start + 50], from_lang, to_lang))
            return result
        aliases: dict[str, str] = {}
        links: dict[str, str | None] = {}
        continuation: dict = {}
        while True:
            data = self._query(from_lang, prop="langlinks", titles="|".join(titles),
                               lllang=to_lang, lllimit="max", **continuation)
            query = data.get("query", {})
            for key in ("normalized", "converted", "redirects"):
                aliases.update((row["from"], row["to"]) for row in query.get(key, []))
            for page in query.get("pages", []):
                links.setdefault(page["title"], None)
                for link in page.get("langlinks", []):
                    if link["lang"] == to_lang:
                        links[page["title"]] = link["title"]
            if "pages" not in query:
                raise UpstreamError("위키백과 언어 링크 응답이 올바르지 않습니다. 잠시 후 다시 시도하세요.")
            continuation = data.get("continue", {})
            if not continuation:
                break
        result = {}
        for original in titles:
            title, seen = original, set()
            while title in aliases and title not in seen:
                seen.add(title)
                title = aliases[title]
            result[original] = links.get(title)
        return result
