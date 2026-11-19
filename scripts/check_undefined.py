"""静态自查：找出函数里"引用了但任何作用域都没定义"的名字。

为什么要有：`s3_script.py` 的 write_script() / revise_script() 在提示词 f-string 里用了
caption_capacity，但函数签名没这个参数 → 第一次生成成功、**返修分支才炸** NameError。
它藏在一段 2000 字提示词中间，只有真跑 S3（要调 VLM 几分钟）才撞得上。

★ 为什么用 symtable 而不是自己走 AST：
  第一版手写作用域分析连错两次 ——
  ① 把函数内部的局部赋值当成模块级名字 → 漏报（main 里有 caption_capacity = ...）；
  ② 不认 lambda 参数与推导式变量 → 满屏误报（64 处）。
  symtable 就是编译器自己的作用域分析，`is_global()` 直接回答"这个引用是不是全局未定义"。

用法：python check_undefined.py [脚本...]   默认扫本目录全部 .py
退出码：0 = 干净；2 = 有问题
"""
import ast
import builtins
import symtable
import sys
from pathlib import Path

KNOWN = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__builtins__',
                              '__package__', '__spec__', '__loader__'}


def line_of(tree, func_name: str, name: str):
    """在指定函数体里找该名字第一次被读取的行号（找不到返回 0）。"""
    best = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == name \
                        and isinstance(sub.ctx, ast.Load):
                    if not best or sub.lineno < best:
                        best = sub.lineno
    return best


def check_file(path: Path):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    top = symtable.symtable(src, str(path), 'exec')
    module_names = {s.get_name() for s in top.get_symbols()} | KNOWN
    problems = []

    def walk(table):
        # 只处理函数作用域；模块级已被 module_names 覆盖
        if table.get_type() != 'module':
            for s in table.get_symbols():
                n = s.get_name()
                if n in module_names:
                    continue
                # 局部绑定 / 形参 / 导入 / 闭包变量 都不算未定义
                if (s.is_parameter() or s.is_assigned() or s.is_imported()
                        or s.is_free() or s.is_local()):
                    continue
                if s.is_global() or s.is_referenced():
                    problems.append((table.get_name(), n))
        for c in table.get_children():
            walk(c)

    walk(top)

    seen, out = set(), []
    for fn, name in problems:
        if (fn, name) in seen:
            continue
        seen.add((fn, name))
        out.append((line_of(tree, fn, name), fn, name))
    return sorted(out)


def main():
    args = sys.argv[1:]
    files = ([Path(a) for a in args] if args
             else sorted(Path(__file__).resolve().parent.glob('*.py')))
    total = 0
    for f in files:
        if f.name == Path(__file__).name or f.suffix != '.py':
            continue
        try:
            bad = check_file(f)
        except SyntaxError as e:
            print(f'[语法错误] {f.name}:{e.lineno}: {e.msg}')
            total += 1
            continue
        for ln, fn, name in bad:
            print(f'{f.name}:{ln}  函数 {fn}() 里用了未定义的 `{name}`')
            total += 1
    print(f'\n共 {total} 处疑似未定义名字')
    return 0 if total == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
