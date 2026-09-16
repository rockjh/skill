#!/usr/bin/env python3
"""按固定门禁顺序运行 E2E；无参数运行全部，--scenario 场景名运行单场景。"""

import sys

sys.dont_write_bytecode = True

from e2e_guard import main_runner


if __name__ == "__main__":
    raise SystemExit(main_runner())
