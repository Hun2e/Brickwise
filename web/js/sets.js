const normalize = value => String(value ?? "").toLocaleLowerCase().replace(/\s+/g, "");

export function searchSets(sets, query, limit = sets.length) {
  const needle = normalize(query);
  return sets.filter(set => [set.set_num, set.name, ...(set.aliases || [])]
    .some(value => normalize(value).includes(needle)))
    .sort((a, b) => Number(b.scope === "core") - Number(a.scope === "core"))
    .slice(0, Math.max(0, limit));
}

export function setDescription(set) {
  return `${set.set_num} · ${set.name} · ${set.year}년 · ${set.num_parts.toLocaleString("ko-KR")}부품${set.scope === "compare_only" ? " · 비교용" : ""}${set.aliases.length ? ` · ${set.aliases.join(", ")}` : ""}`;
}
