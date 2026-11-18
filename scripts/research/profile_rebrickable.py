"""1차 기획 검토 실측용: Rebrickable CSV 프로파일링과 조립 가능성 시뮬레이션.

실행: .venv\Scripts\python scriptsesearch\profile_rebrickable.py dataawebrickable
"""
import sys, pandas as pd
D = sys.argv[1]
r = lambda n: pd.read_csv(f"{D}/{n}.csv.gz")
themes, sets, inv, ip, parts, rel, colors = r("themes"), r("sets"), r("inventories"), r("inventory_parts"), r("parts"), r("part_relationships"), r("colors")
print("rows:", {k: len(v) for k, v in dict(themes=themes, sets=sets, inventories=inv, inventory_parts=ip, parts=parts, part_rel=rel, colors=colors).items()})
print("sets cols:", list(sets.columns)); print("ip cols:", list(ip.columns)); print("rel types:", rel.rel_type.value_counts().to_dict())
# root theme
par = themes.set_index("id").parent_id.to_dict(); name = themes.set_index("id").name.to_dict()
def root(t):
    while pd.notna(par.get(t)): t = int(par[t])
    return t
sets["root"] = sets.theme_id.map(root); sets["root_name"] = sets.root.map(name)
# latest inventory version per set
inv1 = inv.sort_values("version").groupby("set_num").tail(1)
has_parts = set(ip[ip.inventory_id.isin(inv1.id)].inventory_id)
inv1 = inv1[inv1.id.isin(has_parts)]
sets["has_inv"] = sets.set_num.isin(inv1.set_num)
real = sets[sets.num_parts > 0]
summ = real.groupby("root_name").agg(n=("set_num","count"), inv=("has_inv","mean"), y0=("year","min"), y1=("year","max"), med_parts=("num_parts","median")).sort_values("n", ascending=False)
for t in ["Star Wars","Technic","Creator","Ideas","Icons","City","Classic","Harry Potter","Speed Champions","Architecture","Bricklink"]:
    hit = summ[summ.index.str.fullmatch(t)]
    print(t, hit.to_dict("records"))
print("top roots:\n", summ.head(12).to_string())
sw = real[real.root_name=="Star Wars"]
print("Creator subthemes:", real[real.root_name=="Creator"].theme_id.map(name).value_counts().head(8).to_dict())
print("SW subthemes:", sw.theme_id.map(name).value_counts().head(12).to_dict())
print("SW by parts bucket:", pd.cut(sw.num_parts,[0,100,300,1000,10000]).value_counts().sort_index().to_dict())
print("SW year>=2015 & 100..1500 parts:", len(sw[(sw.year>=2015)&(sw.num_parts.between(100,1500))]))
mf = real[real.set_num.str.contains("Falcon", case=False) | real.name.str.contains("Millennium Falcon", case=False)]
print("Millennium Falcon sets:", len(mf), mf[["set_num","name","year","num_parts"]].sort_values("year").head(30).to_string(index=False))
# buildability simulation
ipl = ip.merge(inv1[["id","set_num"]], left_on="inventory_id", right_on="id")
ipl = ipl[ipl.is_spare=="f"] if ipl.is_spare.dtype==object else ipl[~ipl.is_spare]
def need(sn, color=True):
    g = ipl[ipl.set_num==sn]; k = ["part_num","color_id"] if color else ["part_num"]
    return g.groupby(k).quantity.sum()
import numpy as np
rng = np.random.default_rng(0)
cand = sw[(sw.year>=2015)&(sw.num_parts.between(100,1500))&sw.has_inv].set_num.tolist()
targets = sw[(sw.num_parts.between(30,1500))&sw.has_inv].set_num.tolist()
print("candidate owned pool:", len(cand), "targets:", len(targets))
need_c = {s: need(s) for s in targets}; need_n = {s: need(s, False) for s in targets}
for trial in range(3):
    own = rng.choice(cand, 3, replace=False).tolist()
    oc = pd.concat([need(s) for s in own]).groupby(level=[0,1]).sum()
    on = pd.concat([need(s,False) for s in own]).groupby(level=0).sum()
    res=[]
    for t in targets:
        if t in own: continue
        for mode, nd, ow in (("color", need_c[t], oc), ("nocolor", need_n[t], on)):
            have = np.minimum(nd, ow.reindex(nd.index).fillna(0)); res.append((t, mode, have.sum()/nd.sum(), int(nd.sum()-have.sum())))
    df = pd.DataFrame(res, columns=["t","mode","cov","missing"])
    print("owned", own, "owned pieces:", int(oc.sum()))
    for m in ("color","nocolor"):
        d = df[df["mode"]==m]
        print(f"  {m}: >=100%:{(d["cov"]>=1).sum()} >=90%:{(d["cov"]>=.9).sum()} >=75%:{(d["cov"]>=.75).sum()} missing<=20:{(d.missing<=20).sum()}  best:", d.sort_values("cov",ascending=False).head(3)[["t","cov","missing"]].round(2).values.tolist())
