#!/usr/bin/env python3
"""Host checks of the production VS10 HDMI attribute builder; no Kodi/device build."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'xbmc/utils/AMLUtils.cpp').read_text()
start = source.index('      const std::string force_cs_str[]')
end = source.index('      CSysfsPath("/sys/class/amhdmitx/amhdmitx0/attr", fmt_attr);', start)
block = source[start:end]
# Deep Color eligibility is computed outside the string assembly under test.
arrays = block[:block.index('      const bool deep_color')]
builder = block[block.index('      std::string fmt_attr;'):]
preamble = r'''
#include <cassert>
#include <cstdio>
#include <string>
static std::string build(int force_cs, int limit_cd, bool deep_color) {
'''
cases = r'''
    return fmt_attr;
}
int main() {
    const std::string spaces[] = {"", "rgb", "420", "422", "444"};
    const std::string depths[] = {"", "8bit", "10bit", "12bit", "16bit"};
    int checks = 0;
    for (int cs=0; cs<5; cs++) for (int cd=0; cd<5; cd++) for (bool deep : {false,true}) {
        std::string expected = spaces[cs];
        if (deep && cs == 0) expected = "422";
        if (deep || cd) {
            if (!expected.empty()) expected += ",";
            expected += deep ? "12bit" : depths[cd];
        }
        expected += ",now";
        assert(build(cs, cd, deep) == expected);
        checks++;
    }
    // Bare now preserves an existing selection; Auto must explicitly replace it.
    assert(build(0, 0, false) == ",now");
    assert(build(4, 1, false) == "444,8bit,now");
    printf("%d VS10 attribute combinations passed\n", checks);
}
'''
with tempfile.TemporaryDirectory(prefix='kodi-hdmi-attr-') as tmp:
    src = Path(tmp) / 'test.cpp'
    binary = Path(tmp) / 'test'
    src.write_text(preamble + arrays + builder + cases)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-fsanitize=address,undefined', '-o', str(binary), str(src)], check=True)
    subprocess.run([str(binary)], check=True)
