# -*- coding: utf-8 -*-
# 正规包标记（防 import 劫持）：site-packages 里若存在游离的顶层 tests 包，
# 会以正规包身份压过本目录的命名空间包，令 --selftest 的
# `from tests.test_selftest import ...` 改道而 ModuleNotFoundError。
# 本文件使 tests 成为正规包且扫描顺序在前，必赢。2026-09-26 实测。
