# -*- coding: utf-8 -*-
"""runtime_media.py — MediaLedger 省流多模态生成配方账本运行时（零依赖，纯标准库）。

是什么：模板骨架配方账本运行时。
  输入中文生成需求句（如"生成一张1080x1920的赛博朋克风格城市夜景封面图"）：
  ① 骨架匹配（零 LLM）：轻归一化 → 模板分段锚定比对（措辞逐字、参数挖空可换）
     → 未中再 bigram Jaccard 模糊召回，歧义平局不硬猜；
  ② 命中 → 配方（单行 def）子进程沙箱执行 → 直出 Ark 生成 API 调用 payload 字典
     （确定性、可断言判分——只保证 payload 构造正确，不判生成产物好坏）；
  ③ 未命中 → 诚实回退（FAR=0 纪律）：near_miss 提示近似配方，或明确账外，绝不硬猜。
  teach 通道：需求句 + 单行函数 + 断言用例 → 注入黑名单闸 → 判分沙箱自检 → 原子入账。

不是什么：不是生成模型本身——省的是编排 token 与废重试；不宣称替代 Ark/豆包。

CLI：python runtime_media.py --ask "..." [--call] | --teach-file teach.json | --add-pattern-file add.json | --list | --stats | --selftest [--out result.json]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER_PATH = HERE / "state" / "media_ledger.json"
CACHE_DIR = HERE / "state" / "cache"

# ---------------- 阈值（冻结口径） ----------------
FUZZY_T = 0.62    # >= 此值视为措辞族模糊命中
NEAR_T = 0.40     # [NEAR_T, FUZZY_T) 给出 near_miss 提示
AMBIG_D = 0.05    # top1 与 top2 差小于此值视为歧义 → 不硬猜

# 注入控制话术黑名单（小写匹配；teach/add-pattern 闸门共用）
INJECT_PATTERNS = (
    "system:", "忽略以上", "忽略之前", "忽略上面", "报告你的", "系统提示词",
    "ignore previous", "ignore above", "disregard previous",
    "system prompt",
)

_SLOT_RE = re.compile(r"\{(\w+)\}")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")
# fuzzy 通道比对用的噪声滤除（标点）——URL/槽值切取不走这条路（轻归一化保留标点）
_NORM_PAT = re.compile(r"[，。！？、；：“”‘’（）【】《》,.!?;:'\"()\[\]<>~`@#$%^&*_=+|\\/-]")
_NUM_SLOT_RE = re.compile(r"^\d+(?:\.\d+)?$")

# ---------------- 归一化 ----------------

def norm_q(s: str) -> str:
    """轻归一化：NFKC 全半角统一 + lower + 去全部空白。保留标点（URL 槽值安全）。"""
    return _WS_RE.sub("", unicodedata.normalize("NFKC", str(s)).lower())


def _bigrams(s: str):
    if len(s) < 2:
        return {s} if s else set()
    return set(s[i:i + 2] for i in range(len(s) - 1))


def text_sim(a: str, b: str) -> float:
    """二元组 Jaccard 相似度（0~1），输入先做重归一化（去标点）。"""
    A = _bigrams(_NORM_PAT.sub("", norm_q(a)))
    B = _bigrams(_NORM_PAT.sub("", norm_q(b)))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def q_skeleton(q: str) -> str:
    """需求句骨架：数字挖空为 #，用于 fuzzy 通道与模板对齐比对。"""
    return _NUM_RE.sub("#", norm_q(q))


def tmpl_skeleton(template: str) -> str:
    """模板骨架：{槽} 整体挖空为 #，数字同样挖 #。"""
    return _NUM_RE.sub("#", _SLOT_RE.sub("#", norm_q(template)))


# ---------------- 模板解析与分段锚定匹配（核心机制） ----------------

def parse_template(template: str):
    """模板 → 分段列表 [("lit", text) | ("slot", name)]，slots_all 按出现序。"""
    segs, slots_all, pos = [], [], 0
    for m in _SLOT_RE.finditer(template):
        if m.start() > pos:
            segs.append(("lit", norm_q(template[pos:m.start()])))
        segs.append(("slot", m.group(1)))
        slots_all.append(m.group(1))
        pos = m.end()
    if pos < len(template):
        segs.append(("lit", norm_q(template[pos:])))
    return segs, slots_all


def seg_match(segs, q_norm: str, slot_types=None, strict=True):
    """分段锚定匹配：字面段依序出现，槽值取字面段之间文本。

    strict=True（L1 exact）：首/末字面段须与 q 首尾对齐（措辞逐字边界）；
    strict=False（L2 fuzzy 锚定）：仅要求字面段依序出现（容忍句首句尾寒暄）。
    返回 {槽名: 值} 或 None（不匹配/槽值非法/空槽）。数字槽须纯数字。
    """
    slot_types = slot_types or {}
    lits = [(i, v) for i, (k, v) in enumerate(segs) if k == "lit"]
    if not lits:
        return None
    # 首末对齐（strict）：仅当模板首/末分段是字面段时施加（模板以槽开头/结尾则免）
    if strict:
        if segs[0][0] == "lit" and not q_norm.startswith(segs[0][1]):
            return None
        if segs[-1][0] == "lit" and not q_norm.endswith(segs[-1][1]):
            return None
    # 依序 find 所有字面段，记录各槽可取值区间
    spans = []  # (seg_index, start, end)
    pos = 0
    for i, v in lits:
        idx = q_norm.find(v, pos)
        if idx < 0:
            return None
        spans.append((i, idx, idx + len(v)))
        pos = idx + len(v)
    # strict 下字面段覆盖须首尾贴齐（startswith 已保证第一段贴齐；末段贴齐由 endswith）
    vals = {}
    for si, (kind, name) in enumerate(segs):
        if kind != "slot":
            continue
        # 槽区间 = 前一段末尾 .. 后一段开头（首槽从 0 / 末槽到 len(q)）
        left = 0
        right = len(q_norm)
        for (i, s, e) in spans:
            if i < si:
                left = max(left, e)
            elif i > si:
                right = min(right, s)
        seg_val = q_norm[left:right]
        if not seg_val:
            return None
        if slot_types.get(name) == "num" and not _NUM_SLOT_RE.match(seg_val):
            return None
        if name in vals and vals[name] != seg_val:
            return None   # 同名槽取值冲突 → 不硬猜
        vals[name] = seg_val
    return vals


# ---------------- 账本（原子读写） ----------------

def load_ledger(path=None):
    p = Path(path) if path else LEDGER_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def save_ledger(ledger, path=None):
    p = Path(path) if path else LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)   # 原子替换


# ---------------- 沙箱执行（子进程，零网络，5s 超时） ----------------

_RUNNER = r'''
import ast, json, sys
payload = ast.literal_eval(open(sys.argv[1], encoding="utf-8").read().split("=", 1)[1])
ns = {}
try:
    exec(compile(payload["code"], "<recipe>", "exec"), ns)
except Exception as e:
    print(json.dumps({"ok": False, "err": "exec:" + type(e).__name__})); sys.exit(0)
fn = ns.get(payload.get("fn", "f"))
if not callable(fn):
    print(json.dumps({"ok": False, "err": "no_func"})); sys.exit(0)
try:
    got = fn(*payload["args"])
    print(json.dumps({"ok": True, "result": got}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ok": False, "err": "call:" + type(e).__name__}))
'''


def run_recipe(code: str, args, timeout=5):
    """子进程沙箱执行配方单行函数。返回 {"ok", "result"/"err"}。"""
    with tempfile.TemporaryDirectory() as td:
        pay = Path(td) / "pay.txt"
        pay.write_text("pay=" + repr({"code": code, "args": list(args), "fn": "f"}),
                       encoding="utf-8")
        runner = Path(td) / "runner.py"
        runner.write_text(_RUNNER, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, str(runner), str(pay)],
                               capture_output=True, text=True, timeout=timeout,
                               cwd=td, encoding="utf-8", errors="replace")
            out = json.loads(r.stdout.strip() or "{}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
            return {"ok": False, "err": "timeout_or_crash", "result": None}
    return out


# ---------------- 判分（三段断言：精确字段 + prompt_contains + domain） ----------------

def _deep_norm(x):
    if isinstance(x, dict):
        return {k: _deep_norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_deep_norm(v) for v in x]
    return x


def judge_payload(got, expected) -> bool:
    """expected 三段断言：prompt_contains 子串包含 + 其余键递归精确相等；got 多余键不判。"""
    if not isinstance(got, dict):
        return False
    exp = dict(expected)
    contains = exp.pop("prompt_contains", None)
    if contains is not None:
        prompt = got.get("prompt") or ""
        if not all(c and c in prompt for c in contains):
            return False
    return _deep_norm({k: got[k] for k in exp if k in got}) == _deep_norm(exp)


def run_tests(code: str, tests) -> dict:
    """教学自检：每组 [args, expected] 沙箱执行配方并三段判分。"""
    results = []
    for args, expected in tests:
        r = run_recipe(code, args)
        if not r.get("ok"):
            results.append({"ok": False, "err": r.get("err")})
        else:
            results.append({"ok": judge_payload(r["result"], expected)})
    return {"pass": all(x.get("ok") for x in results) and results,
            "n": len(results), "results": results}


# ---------------- ask：骨架匹配三级择向 ----------------

def _all_keys(ledger):
    """(recipe, key) 全展开（含 fam 措辞族）。"""
    for r in ledger["recipes"]:
        for k in r.get("keys") or []:
            yield r, k


def _compile_recipe(r):
    st = r.get("slot_types") or {}
    dom = r.get("domain") or {}
    # fuzzy 通道比对用 exemplar 实例句（tests[0] 按形参序填 key）——
    # 挖槽模板骨架 vs 实例句会丢全部槽词 bigram，天然压低 Jaccard，故比实例句不比骨架。
    base_args = list(r["tests"][0][0]) if r.get("tests") else None
    keys = []
    for k in r.get("keys") or []:
        segs, slots_all = parse_template(k)
        ex = None
        if base_args is not None and len(base_args) == len(r["params"]):
            try:
                ex = k.format(**dict(zip(r["params"], base_args)))
            except Exception:
                ex = None
        keys.append({"key": k, "segs": segs, "slots_all": slots_all, "exemplar": ex})
    return {"id": r["id"], "params": r["params"], "keys": keys,
            "slot_types": st, "domain": dom, "fn": r["fn"], "tests": r.get("tests")}


def ask(ledger, question: str) -> dict:
    """三级择向：L1 分段锚定 exact → L2 模糊召回+锚定 → L3 near_miss/fallback。零 LLM。"""
    qn = norm_q(question)
    # L1：字面段逐字 + 首尾对齐。全收集候选后按"字面段总长"择优——
    # 防短锚贪心吞词（如 r001 末锚"封面图"吞掉 r003"横版封面图"的"横版"）；
    # 同长不同配方 = 歧义，不硬猜（弃 L1 走 L2）。
    cands = []
    for r in ledger["recipes"]:
        c = _compile_recipe(r)
        for kd in c["keys"]:
            vals = seg_match(kd["segs"], qn, c["slot_types"], strict=True)
            if vals is None:
                continue
            args = [vals.get(p) for p in c["params"]]
            if any(a is None for a in args):
                continue
            lit_len = sum(len(v) for k, v in kd["segs"] if k == "lit")
            cands.append((lit_len, r["id"], c, kd, vals, args))
    if cands:
        best = max(x[0] for x in cands)
        top = [x for x in cands if x[0] == best]
        ids = {x[1] for x in top}
        if len(ids) == 1:
            _, _, c, kd, vals, args = top[0]
            out = run_recipe(c["fn"], args)
            audit = _domain_note(c, vals)
            if out.get("ok"):
                return {"status": "direct", "recipe": c["id"], "sim": 1.0,
                        "fuzzy": False, "args": dict(zip(c["params"], args)),
                        "payload": out["result"], "audit": audit}
            return {"status": "error", "recipe": c["id"],
                    "message": "配方沙箱执行失败：" + str(out.get("err"))}
    # L2：bigram Jaccard 模糊召回（exemplar 实例句 vs 需求句；无 exemplar 回退骨架比对）
    scored = []
    for r in ledger["recipes"]:
        c = _compile_recipe(r)
        best_k, best_s = None, -1.0
        for kd in c["keys"]:
            probe = kd["exemplar"] if kd["exemplar"] is not None else kd["key"]
            s = text_sim(probe, question)
            if s > best_s:
                best_k, best_s = kd, s
        if best_k is not None:
            scored.append((best_s, r, best_k))
    scored.sort(key=lambda x: -x[0])
    if scored:
        top1 = scored[0]
        gap = top1[0] - (scored[1][0] if len(scored) > 1 else 0.0)
        if top1[0] >= FUZZY_T and gap >= AMBIG_D:
            c = _compile_recipe(top1[1])
            kd = top1[2]
            # 锚定子序列匹配（容忍句首句尾冗词），槽值仍从用户句实切——不猜
            vals = seg_match(kd["segs"], qn, c["slot_types"], strict=False)
            if vals is not None:
                args = [vals.get(p) for p in c["params"]]
                if all(a is not None for a in args):
                    out = run_recipe(c["fn"], args)
                    if out.get("ok"):
                        return {"status": "direct", "recipe": c["id"],
                                "sim": round(top1[0], 3), "fuzzy": True,
                                "args": dict(zip(c["params"], args)),
                                "payload": out["result"],
                                "audit": _domain_note(c, vals)}
            if top1[0] >= NEAR_T:
                return {"status": "near_miss", "sim": round(top1[0], 3),
                        "closest": {"recipe": top1[1]["id"], "key": kd["key"]},
                        "message": "近似配方但槽位切取失败/歧义，不硬猜。建议按模板措辞重问："}
    if scored and scored[0][0] >= NEAR_T:
        s, r, k = scored[0]
        return {"status": "near_miss", "sim": round(s, 3),
                "closest": {"recipe": r["id"], "key": k},
                "message": "账外措辞，但与既有配方相近。可按此模板重问（参数可换、措辞勿换）："}
    return {"status": "fallback",
            "message": "账外需求——不硬猜。转 LLM/人工，或用 teach_media 教账后秒出。",
            "audit": {"ledger": ledger.get("version"), "n_recipes": len(ledger["recipes"])}}


def _domain_note(c, vals):
    """domain 枚举校验：命中值在推荐枚举内标 in_domain；不在则泛化放行并注明。"""
    notes = {}
    for slot, allowed in (c["domain"] or {}).items():
        v = vals.get(slot)
        if v is None:
            continue
        notes[slot] = ("in_domain" if v in allowed
                       else f"generalized(值 {v} 不在推荐枚举 {allowed}，已放行)")
    return {"domain": notes}


# ---------------- teach：注入闸 → 模板生成 → 沙箱自检 → 原子入账 ----------------

def _inject_check(*texts) -> bool:
    blob = " ".join(str(t) for t in texts).lower()
    return any(p in blob for p in INJECT_PATTERNS)


def make_template(question: str, params, arg_values):
    """需求句 + 形参序值 → 带槽模板：按参数序在原句中定位字面值替换为 {槽名}。

    值须在句中出现（贪心从上一替换点后找，找不到从头再找一次）；缺失 → None。
    """
    q = str(question)
    out, pos = [], 0
    for name, val in zip(params, arg_values):
        if not str(val):
            return None
        hit = q.find(str(val), pos)
        if hit < 0:
            hit = q.find(str(val))
        if hit < 0:
            return None
        out.append(q[pos:hit])
        out.append("{" + name + "}")
        pos = hit + len(str(val))
    out.append(q[pos:])
    return "".join(out)


def teach(ledger, question: str, code: str, tests, patterns=None) -> dict:
    """回填：注入闸 → 语法/单函数闸 → 模板生成 → dup 闸 → 沙箱判分自检 → 入账。"""
    if _inject_check(question, code, json.dumps(tests, ensure_ascii=False),
                     json.dumps(patterns or [], ensure_ascii=False)):
        return {"status": "reject", "reason": "inject_blocked",
                "message": "教学内容命中注入控制话术黑名单，fail-closed 拒入。"}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"status": "reject", "reason": "syntax_error"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(fns) != 1:
        return {"status": "reject", "reason": "need_single_def",
                "message": "配方须为单个顶层函数定义（单行域）。"}
    params = [a.arg for a in fns[0].args.args]
    if not params:
        return {"status": "reject", "reason": "need_params"}
    # tests 结构闸：≥2 组、每组 [args, expected]，args 长度=形参数
    if not isinstance(tests, list) or len(tests) < 2:
        return {"status": "reject", "reason": "need_2_tests"}
    for t in tests:
        if (not isinstance(t, (list, tuple)) or len(t) != 2
                or not isinstance(t[0], (list, tuple)) or len(t[0]) != len(params)):
            return {"status": "reject", "reason": "bad_test_shape",
                    "message": f"tests 每组 [args, expected]，args 长度须={len(params)}。"}
    # 模板生成（tests 第一组的 args 即形参值）
    template = make_template(question, params, list(tests[0][0]))
    if not template:
        return {"status": "reject", "reason": "param_not_in_question",
                "message": "需求句须按形参序包含全部参数字面值（teach 的 question 是带真实参数的原话）。"}
    keys = [template] + [p for p in (patterns or []) if isinstance(p, str) and _SLOT_RE.search(p)]
    # dup 闸：任一 key 骨架与既有配方同骨架 → dup
    for r in ledger["recipes"]:
        for k in list(r.get("keys") or []):
            for nk in keys:
                if tmpl_skeleton(k) == tmpl_skeleton(nk) and set(
                        re.findall(r"\{(\w+)\}", k)) == set(re.findall(r"\{(\w+)\}", nk)):
                    return {"status": "dup", "existing": r["id"],
                            "message": "骨架已在账（措辞族等价），无需重复教学。"}
    # 判分沙箱自检
    verdict = run_tests(code, tests)
    if not verdict["pass"]:
        return {"status": "reject", "reason": "tests_failed",
                "detail": verdict["results"], "message": "判分自检未全过，fail-closed 拒入。"}
    rid = "r%03d" % (len(ledger["recipes"]) + 1)
    # 槽型与域归纳：tests 全数字值 → num 槽；有限值集（≤6）→ 推荐枚举域（仅提示，不拦泛化）
    slot_types, dom = {}, {}
    for i, p in enumerate(params):
        vals = [str(t[0][i]) for t in tests]
        if vals and all(_NUM_SLOT_RE.match(v) for v in vals):
            slot_types[p] = "num"
        if len(set(vals)) <= 6:
            dom[p] = sorted(set(vals))
    ledger["recipes"].append({
        "id": rid, "keys": keys, "fn": code, "params": params,
        "slot_types": slot_types, "domain": dom,
        "tests": tests, "source": "teach:" + time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_ledger(ledger)
    return {"status": "taught", "id": rid, "template": template, "keys": keys,
            "message": "已入账（判分自检 %d/%d 过）。下次同骨架需求秒出。" % (verdict["n"], verdict["n"])}


# ---------------- add-pattern：老配方追加措辞族骨架（fail-closed 闸序） ----------------

def add_pattern(ledger, rid: str, pattern: str, neighbor_probes=None) -> dict:
    """给既有配方追加一条措辞族骨架（teach 只服务新配方，此为老配方唯一入账口）。

    闸序（任一不过即拒，磁盘账本不动）：
      1 id 存在 + pattern 非空含 {槽} + 探针非空
      2 注入黑名单（与 teach 同款）
      3 槽名集合 == 配方 params（不许多槽/少槽/改名）
      4 dup 闸：骨架挖空+槽名集合与全账任一 key 相同 → dup（跨配方同拦）
      5 实例自检：配方自带 tests 逐组套 pattern 成句 → 须 exact(direct) 命中本配方、
        抽参逐位一致、judge_payload 过。验法：内存快照临时 append 新骨架（未落盘）——
        干净账本上无此骨架无从谈起命中；落盘前内存态验证「新骨架真能命中且不与
        既有骨架打架」，失败即摘除，磁盘不动（fail-closed）。
      6 邻居防串台：每条 neighbor_probes 句 ask() 的 recipe（若有）不得 == 本 id
      7 全过才 append + save_ledger 原子写
      8 双保险：重读落盘账本复验实例句 + 探针，败则回滚恢复原账
    """
    probes = [p for p in (neighbor_probes or []) if isinstance(p, str) and p.strip()]
    rec = next((r for r in ledger["recipes"] if r.get("id") == rid), None)
    if rec is None:   # 闸1：id 存在性
        return {"status": "reject", "reason": "no_such_recipe", "id": rid,
                "message": "配方 id 不在账，拒入。"}
    if not isinstance(pattern, str) or not _SLOT_RE.search(pattern):
        return {"status": "reject", "reason": "bad_pattern", "id": rid,
                "message": "pattern 须为含 {槽} 的非空字符串，拒入。"}
    if not probes:
        return {"status": "reject", "reason": "need_neighbor_probe", "id": rid,
                "message": "neighbor_probes 至少 1 条近邻配方实例句，拒入。"}
    if _inject_check(pattern, json.dumps(probes, ensure_ascii=False)):   # 闸2：注入闸
        return {"status": "reject", "reason": "inject_blocked", "id": rid,
                "message": "命中注入控制话术黑名单，fail-closed 拒入。"}
    params = rec["params"]
    slots = set(re.findall(r"\{(\w+)\}", pattern))
    if slots != set(params):   # 闸3：槽位一致性
        return {"status": "reject", "reason": "slot_mismatch", "id": rid,
                "pattern_slots": sorted(slots), "params": params,
                "message": "槽名集合与配方 params 不一致，拒入。"}
    for r in ledger["recipes"]:   # 闸4：dup（与 teach 同款：骨架+槽名集合）
        for k in list(r.get("keys") or []):
            if (tmpl_skeleton(k) == tmpl_skeleton(pattern)
                    and set(re.findall(r"\{(\w+)\}", k)) == slots):
                return {"status": "dup", "id": rid, "existing": r["id"],
                        "message": "骨架已在账（措辞族等价），无需追加。"}
    try:   # 实例化（野括号/缺槽名 → 拒）
        inst_qs = [pattern.format(**dict(zip(params, [str(a) for a in args])))
                   for args, _ in rec["tests"]]
    except (KeyError, IndexError, ValueError):
        return {"status": "reject", "reason": "format_error", "id": rid,
                "message": "pattern format 实例化失败，拒入。"}
    exp_args = [{p: norm_q(str(v)) for p, v in zip(params, args)}
                for args, _ in rec["tests"]]
    # —— 闸5/6：内存快照临时追加（未落盘），败则摘除，磁盘不动 ——
    # args 逐位一致对「新骨架自身的 strict 切取」断言（seg_match）：ask 的 L1 竞争
    # 可能由同配方等价老骨架胜出（如 style 句尾带「风」与老骨架「风的」字面段重叠），
    # 同配方同参同 payload 语义，不构成跨配方歧义/串台；direct+本配方+判分在 ask 层保证。
    segs_new, _ = parse_template(pattern)
    rec["keys"].append(pattern)
    inst_checks, ok_all = [], True
    for (args, expected), q, exp in zip(rec["tests"], inst_qs, exp_args):
        out = ask(ledger, q)
        self_vals = seg_match(segs_new, norm_q(q), rec.get("slot_types") or {},
                              strict=True)
        slice_ok = self_vals == exp
        judge_ok = judge_payload(out.get("payload"), expected)
        ok = (out.get("status") == "direct" and out.get("recipe") == rid
              and slice_ok and judge_ok)
        inst_checks.append({"q": q, "status": out.get("status"),
                            "recipe": out.get("recipe"),
                            "self_slice": self_vals, "slice_ok": slice_ok,
                            "judge_ok": judge_ok, "ok": ok})
        ok_all = ok_all and ok
    probe_checks = []
    if ok_all:
        for p in probes:
            out = ask(ledger, p)
            ok = out.get("recipe") != rid
            probe_checks.append({"q": p, "status": out.get("status"),
                                 "recipe": out.get("recipe"), "ok": ok})
            ok_all = ok_all and ok
    if not ok_all:
        rec["keys"].remove(pattern)
        reason = ("instance_check_failed"
                  if any(not c["ok"] for c in inst_checks) else "neighbor_probe_leak")
        return {"status": "reject", "id": rid, "pattern": pattern, "reason": reason,
                "detail": {"instances": inst_checks, "neighbor_probes": probe_checks},
                "message": "实例自检/邻居防串台未过，已摘除未落盘，fail-closed 拒入。"}
    save_ledger(ledger)   # 闸7：全过才入账（原子写）
    # —— 闸8：双保险，重读落盘账本复验；败则回滚恢复原账 ——
    ledger2 = load_ledger()
    recheck, ok2 = {"instances": [], "probes": []}, True
    for q in inst_qs:
        out = ask(ledger2, q)
        ok = out.get("status") == "direct" and out.get("recipe") == rid
        recheck["instances"].append({"q": q, "status": out.get("status"),
                                     "recipe": out.get("recipe"), "ok": ok})
        ok2 = ok2 and ok
    for p in probes:
        out = ask(ledger2, p)
        ok = out.get("recipe") != rid
        recheck["probes"].append({"q": p, "status": out.get("status"),
                                  "recipe": out.get("recipe"), "ok": ok})
        ok2 = ok2 and ok
    if not ok2:
        rec["keys"].remove(pattern)
        save_ledger(ledger)
        return {"status": "reject", "id": rid, "pattern": pattern,
                "reason": "post_save_recheck_failed", "detail": recheck,
                "message": "落盘复验未过，已回滚，账本恢复原状。"}
    rec2 = next(r for r in ledger2["recipes"] if r["id"] == rid)
    return {"status": "added", "id": rid, "pattern": pattern,
            "selfcheck": {"instances": inst_checks, "neighbor_probes": probe_checks,
                          "post_save_recheck": recheck},
            "n_keys": len(rec2["keys"])}


# ---------------- stats / list ----------------

def stats(ledger) -> dict:
    keys = sum(len(r.get("keys") or []) for r in ledger["recipes"])
    fams = {}
    for r in ledger["recipes"]:
        if len(r.get("keys") or []) > 1:
            fams[r["id"]] = len(r["keys"])
    return {"version": ledger.get("version"),
            "n_recipes": len(ledger["recipes"]),
            "n_keys": keys,
            "n_fam_recipes": len(fams),
            "ids": [r["id"] for r in ledger["recipes"]],
            "ledger_kb": round(LEDGER_PATH.stat().st_size / 1024, 1),
            "adapters": sorted({(r["fn"].split("'")[1] if "'" in r["fn"] else "?")
                                for r in ledger["recipes"]})}


# ---------------- CLI ----------------

def _ask_cli(args):
    ledger = load_ledger()
    out = ask(ledger, args.ask)
    if getattr(args, "adapter", None) and out.get("status") == "direct":
        # 后端覆盖：同一配方 payload 换 adapter 真调（如 ark_image ↔ ark_video）
        out["payload"] = dict(out["payload"], adapter=args.adapter)
        out["adapter_override"] = args.adapter
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.call and out.get("status") == "direct":
        import adapters
        key = os.environ.get("ARK_API_KEY")
        if not key:
            print("ARK_API_KEY 未设置，--call 跳过（不花钱）。", file=sys.stderr)
            return 0
        res = adapters.call_payload(payload, key, cache_dir=CACHE_DIR)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser(description="MediaLedger 省流多模态生成配方账本")
    ap.add_argument("--ask", help="中文生成需求句")
    ap.add_argument("--call", action="store_true", help="真调生成 API（默认 dry 只出 payload）")
    ap.add_argument("--adapter", choices=["ark_image", "ark_video"],
                    help="后端覆盖：payload 的 adapter 字段换此后端再调")
    ap.add_argument("--teach-file", help="教学 JSON 文件：{question, code, tests, patterns?}")
    ap.add_argument("--add-pattern-file",
                    help="老配方追加措辞族骨架 JSON：{id, pattern, neighbor_probes[]}")
    ap.add_argument("--list", action="store_true", help="列全部配方 key 模板")
    ap.add_argument("--stats", action="store_true", help="账本统计")
    ap.add_argument("--selftest", action="store_true", help="跑全部验收判据")
    ap.add_argument("--out", help="selftest 结果 JSON 落盘路径")
    ap.add_argument("--ledger", help="账本路径覆盖（默认 state/media_ledger.json）")
    a = ap.parse_args()
    if a.ask:
        return _ask_cli(a)
    if a.teach_file:
        spec = json.loads(Path(a.teach_file).read_text(encoding="utf-8"))
        out = teach(load_ledger(a.ledger), spec["question"], spec["code"], spec["tests"],
                    spec.get("patterns"))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if a.add_pattern_file:
        spec = json.loads(Path(a.add_pattern_file).read_text(encoding="utf-8"))
        out = add_pattern(load_ledger(a.ledger), spec["id"], spec["pattern"],
                          spec.get("neighbor_probes"))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("status") == "added" else 1
    if a.list:
        for r in load_ledger(a.ledger)["recipes"]:
            for k in r.get("keys") or []:
                print(f"{r['id']}\t{k}")
        return 0
    if a.stats:
        print(json.dumps(stats(load_ledger(a.ledger)), ensure_ascii=False, indent=1))
        return 0
    if a.selftest:
        from tests.test_selftest import run_selftest
        result = run_selftest(a.ledger)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        return 0 if result["verdict"] == "PASS" else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
