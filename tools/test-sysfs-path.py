#!/usr/bin/env python3
"""Exercise production CSysfsPath reads with real filesystem/stream operations.

Only logging and StringUtils::Trim are adapters. Failed reads must yield nullopt;
valid scalar zero and valid empty string must remain distinguishable from failure.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '2a8fd121f87e41f257ead0f5421e3de974b21a9d'

TEST = r'''
#include "SysfsPath.h"
#include <cassert>
#include <iostream>
int failures = 0;
void Check(bool value, const char* name) {
  if (!value) { std::cerr << "failed: " << name << '\n'; ++failures; }
}
void Write(const char* name, const char* value) {std::ofstream(name) << value;}
int main() {
  Write("integer", "3\n"); Write("zero", "0"); Write("negative", "-7");
  Write("decimal", "1.25"); Write("empty", ""); Write("blank", " \n\t");
  Write("invalid", "unavailable"); Write("overflow", "99999999999999999999999");
  Write("text", "  available\nsecond line  \n");
  const auto good = CSysfsPath("integer").Get<int>();
  Check(good && *good == 3, "valid integer");
  const auto zero = CSysfsPath("zero").Get<unsigned int>();
  Check(zero && *zero == 0, "known zero");
  const auto negative = CSysfsPath("negative").Get<int>();
  Check(negative && *negative == -7, "signed integer");
  const auto decimal = CSysfsPath("decimal").Get<float>();
  Check(decimal && *decimal == 1.25f, "float");
  for (const char* path : {"missing", "empty", "blank", "invalid", "overflow"}) {
    Check(!CSysfsPath(path).Get<unsigned int>(), path);
    Check(!CSysfsPath(path).Get<int>(), path);
  }
  Check(!CSysfsPath("missing").Get<std::string>(), "missing string");
  const auto empty = CSysfsPath("empty").Get<std::string>();
  Check(empty && empty->empty(), "valid empty string");
  const auto text = CSysfsPath("text").Get<std::string>();
  Check(text && *text == "available\nsecond line", "trimmed multiline string");
  std::filesystem::create_directory("directory");
  Check(!CSysfsPath("directory").Get<int>(), "directory scalar read");
  Check(!CSysfsPath("directory").Get<std::string>(), "directory string read");
  if (failures) return 1;
  std::cout << "sysfs read contracts passed\n";
}
'''


def run(folder, header, implementation):
    folder.mkdir(parents=True)
    (folder / 'utils').mkdir()
    (folder / 'utils/log.h').write_text('constexpr int LOGERROR=0; struct CLog {template<class... T> static void LogF(T&&...) {}};\n')
    (folder / 'utils/StringUtils.h').write_text(r'''
#include <string>
struct StringUtils {static std::string& Trim(std::string& value) {
  auto first=value.find_first_not_of(" \t\r\n");
  if(first==std::string::npos) {value.clear();return value;}
  value=value.substr(first,value.find_last_not_of(" \t\r\n")-first+1);return value;
}};
''')
    (folder / 'SysfsPath.h').write_text(header)
    (folder / 'SysfsPath.cpp').write_text(implementation)
    (folder / 'test.cpp').write_text(TEST)
    subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                    '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    '-I', str(folder), str(folder / 'test.cpp'), str(folder / 'SysfsPath.cpp'),
                    '-o', str(folder / 'test')], check=True)
    return subprocess.run([str(folder / 'test')], cwd=folder, capture_output=True, text=True,
                          timeout=10, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                                           'UBSAN_OPTIONS': 'halt_on_error=1'})


def main(baseline, controls):
    paths = ['xbmc/platform/linux/SysfsPath.h', 'xbmc/platform/linux/SysfsPath.cpp']
    source = [subprocess.check_output(['git', 'show', BASELINE + ':' + path], cwd=ROOT, text=True)
              if baseline else (ROOT / path).read_text() for path in paths]
    with tempfile.TemporaryDirectory(prefix='sysfs-reads-') as scratch:
        result = run(Path(scratch) / 'positive', *source)
        if baseline:
            assert result.returncode == 1 and 'failed: missing' in result.stderr, result.stderr
            print('Original failed-read contract reproduced:', result.stderr.strip())
            return
        assert result.returncode == 0, result.stderr
        print(result.stdout.strip())
        if controls:
            for label, index, old, new in [
                ('scalar failure', 0, 'if (!(file >> value))', 'file >> value; if (file.bad())'),
                ('string open failure', 1, '!file.is_open() || file.bad()', 'file.bad()'),
                ('known zero', 0, 'return value;', 'if (value == T{}) { return std::nullopt; }\n      return value;'),
                ('valid empty string', 1, 'return StringUtils::Trim(value);', 'if (value.empty()) { return std::nullopt; }\n    return StringUtils::Trim(value);')]:
                changed = source.copy()
                assert old in changed[index], label
                changed[index] = changed[index].replace(old, new)
                result = run(Path(scratch) / label.replace(' ', '-'), *changed)
                assert result.returncode == 1 and 'failed:' in result.stderr, (label, result.stderr)
                print('Rejected control:', label)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--controls', action='store_true')
    args = parser.parse_args()
    main(args.baseline, args.controls)
