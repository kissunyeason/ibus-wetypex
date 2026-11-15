#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rime2wetypex.py —— 把雾凇(Rime)个人词库同步到 ibus-wetypex 微信输入法

默认「只预览不写入」：解析词库、统计、打印示例，不动任何文件。
确认无误后加 --apply 才真正导入。

两种导入通道（--mode）：
  phrases  写 ~/.config/wetypex/dynamic-phrases.txt（v6+ 自带，10 秒热重载，
           零重启、零风险，删掉文件即回滚）。一个输入码对应一个词，
           同码多词保留词频最高的那条。
  hotword  通过 control.sock 写进原版核心的「常用语」，设置窗口可见可编辑，
           能跟随官方设备同步，支持一码多词，最多 10000 条。
  both     两个都做。

用法示例：
  python3 rime2wetypex.py                          # 预览（自动找雾凇词库）
  python3 rime2wetypex.py --apply                 # 导入到 phrases
  python3 rime2wetypex.py --mode hotword --apply  # 写入原版核心常用语
  python3 rime2wetypex.py --input ~/rime_export.txt --apply
  python3 rime2wetypex.py --respell --apply        # 双拼/注音方案重新标全拼
"""

import argparse
import glob
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time
from collections import defaultdict

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# v6 动态候选内置的触发码。customPhrases() 命中后会直接 return，
# 所以词库里若含这些码，会盖掉日期/农历/时间/计算器候选。
BUILTIN_CODES = {"rq", "date", "nl", "lunar", "sj", "time", "xq", "week"}

RIME_DIRS = [
    "~/.config/ibus/rime",          # ibus-rime / 雾凇，最常见
    "~/.local/share/fcitx5/rime",   # fcitx5-rime
    "~/.config/fcitx/rime",
    "~/.local/share/rime",
    "~/.config/rime",
]

RIME_MANAGER_CANDIDATES = [
    "rime_dict_manager",
    "/usr/bin/rime_dict_manager",
    "/usr/lib/rime/rime_dict_manager",
]


def expand(p):
    return pathlib.Path(os.path.expanduser(str(p)))


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- 发现词库
def find_rime_dir(explicit=None):
    if explicit:
        p = expand(explicit)
        return p if p.is_dir() else None
    for d in RIME_DIRS:
        p = expand(d)
        if p.is_dir() and (list(p.glob("*.userdb")) or list(p.glob("*.userdb.txt"))
                           or (p / "installation.yaml").exists()):
            return p
    return None


TEXT_NAMES = ("custom_phrase.txt", "custom_phrase_double.txt",
              "custom_phrase_t9.txt", "user_dict.txt", "userdb_export.txt")
SKIP_SUFFIX = (".dict.yaml", ".schema.yaml", ".custom.yaml", ".md", ".lua",
               ".json", ".bib", ".bin")


def find_snapshots(rime_dir):
    """找现成的纯文本词库源，完全不需要 rime_dict_manager。

    优先级：custom_phrase*.txt（用户自定义短语）> sync/**/*.userdb.txt（同步快照）
            > 目录根下的 *.userdb.txt / *.tsv。
    系统词库 *.dict.yaml / *.schema.yaml 一律排除（几十万条，不是个人词库）。
    """
    found = []
    dirs = [rime_dir]
    inst = rime_dir / "installation.yaml"
    if inst.exists():
        try:
            for line in inst.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*sync_dir\s*:\s*['\"]?([^'\"#]+)", line)
                if m:
                    d = expand(m.group(1).strip())
                    if d.is_dir():
                        dirs.append(d)
        except OSError:
            pass

    # 1. 自定义短语（雾凇 custom_phrase.txt 是纯文本，直接可读）
    for name in TEXT_NAMES:
        for d in dirs:
            p = d / name
            if p.is_file() and p.stat().st_size > 0:
                found.append(p)
    # 2. 同步快照（sync/<installation_id>/*.userdb.txt）
    for d in dirs:
        for p in d.glob("**/*.userdb.txt"):
            if p.is_file() and p.stat().st_size > 0:
                found.append(p)
    # 3. 根目录 / 常见下载位置的导出文本
    for p in list(rime_dir.glob("*.userdb.txt")) + list(rime_dir.glob("*.tsv")):
        if p.is_file() and p.stat().st_size > 0:
            found.append(p)
    for pat in ("~/*.userdb.txt", "~/下载/*.userdb.txt", "~/Downloads/*.userdb.txt",
                "~/rime_export/*.txt", "~/rime_export/*.tsv"):
        for x in glob.glob(os.path.expanduser(pat)):
            p = expand(x)
            if p.is_file() and p.stat().st_size > 0 and not p.name.endswith(SKIP_SUFFIX):
                found.append(p)
    # 去重
    seen, out = set(), []
    for p in found:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen and not p.name.endswith(SKIP_SUFFIX):
            seen.add(key)
            out.append(p)
    return out


def manager_path():
    for c in RIME_MANAGER_CANDIDATES:
        p = shutil.which(c) if not c.startswith("/") else c
        if p and pathlib.Path(p).exists() and os.access(p, os.X_OK):
            return p
    return None


def export_via_manager(rime_dir, workdir):
    """导出二进制 *.userdb 为三列 TSV。

    关键点：Rime 运行时会独占 userdb（LevelDB LOCK），直接 -e 会报 read-only。
    这里先把 *.userdb/ 复制到临时目录、删掉 LOCK，再在副本上导出，
    所以**不需要退出输入法、不需要切英文**。
    """
    tool = manager_path()
    if not tool:
        return [], "未找到 rime_dict_manager（Fedora: sudo dnf install -y librime-tools）"

    dbs = sorted(d for d in rime_dir.glob("*.userdb") if d.is_dir())
    if not dbs:
        return [], "目录下没有 *.userdb 二进制词典"
    names = [d.name[:-len(".userdb")] for d in dbs]

    sandbox = workdir / "rime-copy"
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True)
    for d in dbs:
        try:
            shutil.copytree(d, sandbox / d.name)
            for lock in (sandbox / d.name).glob("LOCK"):
                lock.unlink(missing_ok=True)
        except OSError as e:
            return [], "复制 %s 失败: %s" % (d.name, e)
    inst = rime_dir / "installation.yaml"
    if inst.exists():
        shutil.copy2(inst, sandbox / "installation.yaml")

    out, errors = [], []
    for name in names:
        dst = workdir / ("%s.tsv" % name)
        try:
            r = subprocess.run([tool, "-e", name, str(dst)], cwd=str(sandbox),
                               capture_output=True, text=True, timeout=180)
            if dst.is_file() and dst.stat().st_size > 0:
                out.append(dst)
            else:
                errors.append("%s: %s" % (name, (r.stderr or r.stdout or "无输出").strip()[:150]))
        except (OSError, subprocess.SubprocessError) as e:
            errors.append("%s: %s" % (name, e))
    hint = "已在临时副本上导出，原词库未被修改；若个别词典失败可稍后重跑。"
    return out, ("; ".join(errors) + " | " + hint) if errors else ""


# ---------------------------------------------------------------- 解析
def parse_freq(s):
    """兼容 '123' / '2.5' / 'c=5 d=2.5 t=1640000000' / 空。"""
    s = (s or "").strip()
    m = re.search(r"\bc=([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        return float(m.group(1))
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        return float(m.group(1))
    return 0.0


def norm_code(c):
    """Rime 编码 -> 微信输入码：去辅助码/形码、声调数字、空格与隔音符，只留 a-z。"""
    c = (c or "").lower().strip()
    c = c.split(";")[0].split("/")[0]
    c = re.sub(r"[0-9]", "", c)
    c = re.sub(r"[^a-z]", "", c)
    return c


def parse_one(path, stats, include_ascii=False, respell=False):
    """返回 [(code, text, freq), ...]；自动识别列序、TSV/空格分隔、快照与码表。"""
    rows = []
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        stats["errors"].append("%s: %s" % (path, e))
        return rows

    # 检测列序：抽样前 50 条有效行，若第 1 列多为 ASCII 而第 2 列含汉字 -> 编码在前
    sample = []
    for line in raw.splitlines():
        s = line.rstrip("\r")
        if not s or s.startswith("#") or s.startswith("---") or s.startswith("..."):
            continue
        cols = s.split("\t") if "\t" in s else s.split()
        if len(cols) >= 2:
            sample.append(cols)
        if len(sample) >= 50:
            break
    swapped = 0
    for c in sample:
        first_has_cjk, second_has_cjk = bool(CJK.search(c[0])), bool(CJK.search(c[1]))
        if not first_has_cjk and second_has_cjk:
            swapped += 1
    flip = bool(sample) and swapped * 2 >= len(sample) and swapped > 0

    pypinyin = None
    if respell:
        try:
            from pypinyin import lazy_pinyin
            pypinyin = lazy_pinyin
        except ImportError:
            stats["errors"].append("未安装 pypinyin，--respell 已忽略（可选：pip3 install pypinyin）")

    for line in raw.splitlines():
        s = line.rstrip("\r")
        stats["lines"] += 1
        if not s or s.startswith("#") or s.startswith("---") or s.startswith("..."):
            continue
        cols = s.split("\t") if "\t" in s else s.split()
        if len(cols) < 2:
            stats["skip_cols"] += 1
            continue
        text, code = cols[0].strip(), cols[1].strip()
        if flip:
            text, code = code, text
        freq = parse_freq(cols[2] if len(cols) > 2 else "")
        stats["parsed"] += 1

        if not include_ascii and not CJK.search(text):
            stats["skip_no_cjk"] += 1
            continue
        if pypinyin:
            try:
                code = "".join(pypinyin(text))
            except Exception:
                pass
        code = norm_code(code)
        if not code or len(code) > 40 or len(text) > 64:
            stats["skip_bad_code"] += 1
            continue
        if text.isspace() or not text:
            stats["skip_bad_code"] += 1
            continue
        rows.append((code, text, freq))
    stats["files"].append((str(path), len(rows)))
    return rows


# ---------------------------------------------------------------- 输出
def write_phrases(entries, out_path, sources, dry_run):
    lines = [
        "# 由 rime2wetypex.py 从雾凇/Rime 用户词库生成 %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "# 来源: %s" % ", ".join(sources[:5]),
        "# 共 %d 条；格式：输入码<TAB>文本。修改后 10 秒自动生效，无需重启。" % len(entries),
        "# 说明：一个输入码只保留词频最高的一条；删除本文件即完整回滚。",
    ]
    lines.extend("%s\t%s" % (c, t) for c, t, _ in entries)
    content = "\n".join(lines) + "\n"
    if dry_run:
        log("\n[预览] 将写入 %s（%d 字节，未实际写入）" % (out_path, len(content.encode())))
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        bak = out_path.with_name(out_path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(out_path, bak)
        log("  已备份原文件 -> %s" % bak)
    out_path.write_text(content, encoding="utf-8")
    log("  已写入 %s（%d 条）" % (out_path, len(entries)))
    return out_path


def check_engine_binary():
    """确认装的是带动态短语层的 -6 及以后版本，否则写入了也不会生效。"""
    import subprocess as sp
    binpath = None
    for c in ("/usr/libexec/ibus-engine-wetypex", "/usr/lib/ibus/ibus-engine-wetypex",
              "/usr/bin/ibus-engine-wetypex"):
        if pathlib.Path(c).is_file():
            binpath = c
            break
    if not binpath:
        try:
            r = sp.run(["rpm", "-ql", "ibus-wetypex"], capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                if line.endswith("ibus-engine-wetypex") and pathlib.Path(line).is_file():
                    binpath = line
                    break
        except (OSError, sp.SubprocessError):
            pass
    if not binpath:
        log("  ? 未找到 ibus-engine-wetypex，跳过版本检查")
        return True
    try:
        data = pathlib.Path(binpath).read_bytes()
    except OSError:
        return True
    ok = b"lunarCandidates" in data or b"dynamicCandidatesFor" in data
    if not ok:
        log("  ! 当前引擎二进制缺少动态候选层，dynamic-phrases.txt 不会生效。")
        log("    请先安装 v3.0.1.684-6 或更新版本（rpm -q ibus-wetypex 查看版本）。")
    else:
        log("  ✓ 引擎已带动态候选层，短语表可用")
    return ok


def socket_roundtrip(path, payload, timeout=15):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk or len(buf) > 16 * 1024 * 1024:
                raise RuntimeError("控制通道返回异常")
            buf.extend(chunk)
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))


def write_hotwords(entries, sock_path, dry_run, limit):
    if not sock_path.exists():
        raise RuntimeError("找不到控制通道 %s；请确认已启用 WeTypeX 输入法并切换到它一次" % sock_path)
    base = {"session": 1, "seq": int(time.time() * 1000), "epoch": 1}
    r = socket_roundtrip(sock_path, dict(base, op="hotword_list"))
    if r.get("error"):
        raise RuntimeError("hotword_list: %s" % r["error"])
    existing = {str(x.get("key", "")) for x in r.get("hotwords", [])}
    todo = [(c, t) for c, t, _ in entries if c not in existing][:limit]
    skipped = sum(1 for c, t, _ in entries if c in existing)
    log("  核心已有常用语 %d 条；本次新增 %d 条，跳过重复 %d 条" %
        (len(existing), len(todo), skipped))
    if dry_run:
        log("[预览] 未写入；将逐条发送 hotword_set（不实际执行）")
        return 0
    done = 0
    for i, (code, text) in enumerate(todo, 1):
        req = dict(base, op="hotword_set", id="", key=code, words=text)
        req["seq"] = int(time.time() * 1000) + i
        try:
            resp = socket_roundtrip(sock_path, req, timeout=20)
            if resp.get("error"):
                log("  ! %s -> %s : %s" % (code, text, resp["error"]))
            else:
                done += 1
        except Exception as e:
            log("  ! %s : %s" % (code, e))
        if i % 100 == 0:
            log("  进度 %d/%d" % (i, len(todo)))
    log("  成功写入 %d 条" % done)
    return done


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="把雾凇(Rime)个人词库同步到 ibus-wetypex",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", action="append", default=[],
                    help="手动指定 Rime 导出文件（.tsv/.txt/.userdb.txt/.dict.yaml），可多次；跳过自动发现")
    ap.add_argument("--rime-dir", help="手动指定 Rime 用户目录")
    ap.add_argument("--export-db", action="store_true",
                    help="连二进制 *.userdb 一起导出（自动复制到临时目录，无需停输入法；需 librime-tools）")
    ap.add_argument("--mode", choices=["phrases", "hotword", "both"], default="phrases")
    ap.add_argument("--limit", type=int, default=10000, help="最多导入条数（默认 10000，核心上限）")
    ap.add_argument("--min-freq", type=float, default=0.0, help="只导入词频 >= 此值的词条")
    ap.add_argument("--respell", action="store_true", help="用 pypinyin 从汉字重标全拼（双拼/注音方案必开）")
    ap.add_argument("--include-ascii", action="store_true", help="保留纯英文/数字词条")
    ap.add_argument("--apply", action="store_true", help="真正写入；不加则只预览")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--phrases-path", help="覆盖 dynamic-phrases.txt 路径")
    ap.add_argument("--socket-path", help="覆盖 control.sock 路径")
    args = ap.parse_args()

    dry = not args.apply
    workdir = expand("~/.cache/wetypex-dict-sync")
    workdir.mkdir(parents=True, exist_ok=True)

    log("=" * 68)
    log("雾凇(Rime) 词库 -> ibus-wetypex  %s" % ("【预览模式，不写入】" if dry else "【写入模式】"))
    log("=" * 68)

    # 1. 收集源文件
    files = []
    for item in args.input:
        p = expand(item)
        if p.is_dir():
            # 传的是 Rime 目录时按同样规则找现成文本源，避免误吞系统词库
            texts = find_snapshots(p)
            if texts:
                files.extend(texts)
            else:
                for pat in ("*.txt", "*.tsv", "*.userdb.txt"):
                    for q in sorted(p.glob(pat)):
                        if q.is_file() and not q.name.endswith(SKIP_SUFFIX):
                            files.append(q)
        elif p.is_file():
            files.append(p)
        else:
            log("! 找不到输入: %s" % p)
    if not files or args.export_db:
        rime_dir = find_rime_dir(args.rime_dir)
        if not rime_dir:
            log("! 未自动发现 Rime 用户目录。请用 --rime-dir 或 --input 指定。")
            log("  常见位置：~/.config/ibus/rime  ~/.local/share/fcitx5/rime")
            return 2
        log("Rime 用户目录: %s" % rime_dir)
        texts = [q for q in find_snapshots(rime_dir) if q not in files]
        if texts:
            log("现成文本词库源 %d 个（无需停输入法、无需装工具）:" % len(texts))
            for q in texts[:10]:
                log("  %s" % q)
            files.extend(texts)
        bins = sorted(rime_dir.glob("*.userdb"))
        if args.export_db or not texts:
            log("导出二进制用户词典（复制到临时目录，原词库不动）…")
            exported, err = export_via_manager(rime_dir, workdir)
            files.extend(exported)
            if exported:
                log("  已导出: %s" % ", ".join(x.name for x in exported))
            if not exported and not files:
                log("! 自动导出失败：%s" % (err or "未知原因"))
                log("  请运行: sh export-rime.sh  （会自动装 librime-tools）")
                return 2
            if err:
                log("  提示: %s" % err)
        elif bins:
            log("  还有 %d 个二进制用户词典未包含：%s" %
                (len(bins), ", ".join(x.name for x in bins)))
            log("  要连它们一起导，加 --export-db（需 librime-tools），或跑 sh export-rime.sh")
    if not files:
        log("! 没有可解析的词库文件")
        return 2

    # 2. 解析
    stats = defaultdict(int)
    stats["files"] = []
    stats["errors"] = []
    rows = []
    for p in files:
        rows.extend(parse_one(pathlib.Path(p), stats, args.include_ascii, args.respell))
    log("\n解析结果:")
    for f, n in stats["files"]:
        log("  %-62s %6d 条" % (f[-62:], n))
    log("  %-62s %6d 行" % ("总行数", stats["lines"]))
    log("  有效词条 %d | 列数不足 %d | 非汉字跳过 %d | 编码无效 %d" %
        (stats["parsed"], stats["skip_cols"], stats["skip_no_cjk"], stats["skip_bad_code"]))
    for e in stats["errors"]:
        log("  ! " + e)
    if not rows:
        log("! 没有解析出任何词条，请检查文件格式（应为 Tab 分隔：文字 / 编码 / 频次）")
        return 2

    # 3. 按词频排序、同码去重
    rows.sort(key=lambda x: -x[2])
    merged = {}
    dup = 0
    for c, t, f in rows:
        if f < args.min_freq:
            continue
        if c in merged:
            dup += 1
            continue
        merged[c] = (c, t, f)
    entries = sorted(merged.values(), key=lambda x: x[0])[:args.limit]
    log("  同码合并 %d 条 | 词频过滤(--min-freq %.1f) 后 %d 条 | 最终 %d 条" %
        (dup, args.min_freq, len(merged), len(entries)))

    conflicts = [(c, t) for c, t, _ in entries if c in BUILTIN_CODES or c.startswith("=")]
    if conflicts:
        log("\n! 输入码冲突 %d 条（会盖掉 v6 内置的日期/农历/时间/计算器候选）:" % len(conflicts))
        for c, t in conflicts[:10]:
            log("    %-12s -> %s" % (c, t))
        log("  雾凇 custom_phrase.txt 常用 rq/nl/sj 这类简码，属于正常现象。")
        log("  想保留两者：把该行的码改掉（如 rq -> rq0），或让我改一版 -7 让候选合并。")

    log("\n示例（按输入码排序，前 15 条）:")
    log("  %-22s %-24s %s" % ("输入码", "词", "词频"))
    for c, t, f in entries[:15]:
        log("  %-22s %-24s %s" % (c, t, f))

    src_args = " ".join("--input '%s'" % x for x in args.input)
    if args.rime_dir:
        src_args += " --rime-dir '%s'" % args.rime_dir
    suffix = (" " + src_args.strip()) if src_args.strip() else ""
    if dry:
        log("\n以上为预览，未写入任何文件。确认无误后加 --apply 执行。")
        log("  导入前端短语（推荐，零重启）: python3 %s%s --apply" % (sys.argv[0], suffix))
        log("  写入原版核心常用语          : python3 %s%s --mode hotword --apply" % (sys.argv[0], suffix))
        return 0

    if not args.yes:
        print("\n即将写入 %d 条词条（模式: %s）。继续? [y/N] " % (len(entries), args.mode), end="", flush=True)
        try:
            if input().strip().lower() not in ("y", "yes"):
                log("已取消")
                return 1
        except EOFError:
            pass

    log("\n开始导入…")
    cfg = pathlib.Path(os.path.expanduser(args.phrases_path or
                        "~/.config/wetypex/dynamic-phrases.txt"))
    state = pathlib.Path(os.path.expanduser(args.socket_path or
                         "~/.local/share/wetypex/state/control.sock"))
    sources = [f for f, _ in stats["files"]]

    if args.mode in ("phrases", "both"):
        log("[1] 前端自定义短语 -> %s" % cfg)
        check_engine_binary()
        write_phrases(entries, cfg, sources, False)
        log("    10 秒后自动生效；若需立即生效可 ibus restart。")
    if args.mode in ("hotword", "both"):
        log("[2] 原版核心常用语 -> %s" % state)
        try:
            write_hotwords(entries, state, False, args.limit)
        except Exception as e:
            log("  ! 写入核心失败: %s" % e)
            log("    可先用 --mode phrases 走前端通道。")
            return 3
    log("\n完成。切到 WeTypeX 输入一个词库里的码验证。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已中断")
        sys.exit(130)
