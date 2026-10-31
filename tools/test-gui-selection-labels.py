#!/usr/bin/env python3
"""Host test of which window image labels follow the selected list item.

Compiles the production CGUIInfoLabel::IsListItemDependent,
CGUIInfoManager::IsListItemDependentInfo/IsListItemInfo and
CSkinVariableString::IsListItemDependent, with the info ID ranges read from
GUIInfoLabels.h, against owned label, condition and skin variable fixtures.
A label part whose translation reported a list item argument is given here
directly; CGUIInfoLabel::Parse, which records it, is not executed.
Semantic mutations must fail their specific assertions. Label parsing, info
providers and actual skins are not executed.
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'xbmc/guilib/guiinfo/GUIInfoLabel.cpp'
MANAGER = 'xbmc/GUIInfoManager.cpp'
VARIABLE = 'xbmc/interfaces/info/SkinVariable.cpp'
LABELS = 'xbmc/guilib/guiinfo/GUIInfoLabels.h'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"', lambda m: ' ' * len(m.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <algorithm>
#include <cstdio>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
@RANGES@
using CCriticalSection = std::recursive_mutex;
namespace INFO {
struct InfoBool {bool dependent=false;bool ListItemDependent()const{return dependent;}};
using InfoPtr = std::shared_ptr<InfoBool>;
}
class CGUIInfoLabel {public:
 struct CInfoPortion {int m_info;bool m_listItemDependent=false;};
 explicit CGUIInfoLabel(std::vector<int> label={},std::vector<int> fallback={}){for(int i:label)m_infoLabel.push_back({i});for(int i:fallback)m_infoFallback.push_back({i});}
 bool IsListItemDependent() const;
 std::vector<CInfoPortion> m_infoLabel,m_infoFallback;mutable std::optional<bool> m_listItemDependent;};
class CSkinVariableString {public:
 struct ConditionLabelPair {INFO::InfoPtr m_condition;CGUIInfoLabel m_label;};
 std::vector<ConditionLabelPair> m_conditionLabelPairs;bool IsListItemDependent() const;};
class CGUIInfoManager {public:
 std::vector<int> multi;std::vector<CSkinVariableString> m_skinVariableStrings;mutable CCriticalSection m_critInfo;
 int GetMultiInfoValue(int id)const{return multi.at(id-MULTI_INFO_START);}
 bool IsListItemInfo(int info) const;bool IsListItemDependentInfo(int info) const;};
static CGUIInfoManager manager;
struct GUI {CGUIInfoManager&GetInfoManager(){return manager;}};
struct CServiceBroker {static GUI*GetGUI(){static GUI gui;return &gui;}};
'''

TESTS = r'''
static unsigned checks=0;
static void Check(bool ok,const char*message){++checks;if(!ok)throw std::runtime_error(message);}
static CSkinVariableString Variable(bool condition,std::vector<int> label){CSkinVariableString v;
 v.m_conditionLabelPairs.push_back({condition?std::make_shared<INFO::InfoBool>(INFO::InfoBool{true}):std::make_shared<INFO::InfoBool>(),CGUIInfoLabel(label)});return v;}
static bool Follows(std::vector<int> label,std::vector<int> fallback={}){return CGUIInfoLabel(label,fallback).IsListItemDependent();}
int main(){try{
 const int listItem=LISTITEM_START+3,player=66;
 manager.multi={listItem,player};
 manager.m_skinVariableStrings={Variable(true,{player}),Variable(false,{listItem}),Variable(false,{player}),Variable(false,{CONDITIONAL_LABEL_START+1})};
 Check(Follows({listItem}),"list item info follows the selection");
 Check(!Follows({player}),"playback info does not follow the selection");
 Check(!Follows({0}),"plain text does not follow the selection");
 Check(Follows({MULTI_INFO_START}),"container list item info follows the selection");
 Check(!Follows({MULTI_INFO_START+1}),"container playback info does not follow the selection");
 Check(Follows({CONDITIONAL_LABEL_START}),"variable with a list item condition follows the selection");
 Check(Follows({CONDITIONAL_LABEL_START+1}),"variable with a list item label follows the selection");
 Check(!Follows({CONDITIONAL_LABEL_START+2}),"variable of playback info does not follow the selection");
 Check(Follows({CONDITIONAL_LABEL_START+3}),"nested variable with a list item label follows the selection");
 Check(!Follows({CONDITIONAL_LABEL_START+4}),"unknown variable does not follow the selection");
 Check(Follows({player},{listItem}),"list item fallback follows the selection");
 CGUIInfoLabel computed;computed.m_infoLabel.push_back({MULTI_INFO_START+1,true});
 Check(computed.IsListItemDependent(),"computed info with a list item argument follows the selection");
 CGUIInfoLabel cached({player});Check(!cached.IsListItemDependent(),"cached label starts as playback");
 cached.m_infoLabel={{listItem}};Check(!cached.IsListItemDependent(),"label result is cached after the first check");
 std::printf("PASS %u selection label checks\n",checks);return 0;}catch(const std::exception&e){std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}}
'''


def source(files):
    labels = files[LABELS]
    ranges = []
    for name in ('LISTITEM_START', 'LISTITEM_END', 'CONDITIONAL_LABEL_START', 'CONDITIONAL_LABEL_END',
                 'MULTI_INFO_START', 'MULTI_INFO_END'):
        ranges.append(re.search(r'#define ' + name + r'\b[^\n]*', labels).group(0).split('//')[0])
    body = [function(files[LABEL], 'bool CGUIInfoLabel::IsListItemDependent('),
            function(files[MANAGER], 'bool CGUIInfoManager::IsListItemInfo('),
            function(files[MANAGER], 'bool CGUIInfoManager::IsListItemDependentInfo('),
            function(files[VARIABLE], 'bool CSkinVariableString::IsListItemDependent(')]
    return PRELUDE.replace('@RANGES@', '\n'.join(ranges)) + '\n\n'.join(body) + '\n' + TESTS


MUTATIONS = [
    ('variable-ignored', 'if (info < CONDITIONAL_LABEL_START || info > CONDITIONAL_LABEL_END)', 'if (true)',
     'variable with a list item condition follows the selection'),
    ('playback-follows', '    return false;\n\n  info -= CONDITIONAL_LABEL_START;', '    return true;\n\n  info -= CONDITIONAL_LABEL_START;',
     'playback info does not follow the selection'),
    ('condition-ignored', '(pair.m_condition && pair.m_condition->ListItemDependent()) ||', '',
     'variable with a list item condition follows the selection'),
    ('variable-label-ignored', ' ||\n                              pair.m_label.IsListItemDependent()', '',
     'variable with a list item label follows the selection'),
    ('fallback-ignored', ' ||\n                          std::any_of(m_infoFallback.begin(), m_infoFallback.end(), dependent)', '',
     'list item fallback follows the selection'),
    ('multi-not-resolved', 'while (iResolvedInfo >= MULTI_INFO_START && iResolvedInfo <= MULTI_INFO_END)', 'while (false)',
     'container list item info follows the selection'),
    ('argument-flag-ignored', '      return portion.m_listItemDependent ||\n             (portion.m_info && infoMgr.IsListItemDependentInfo(portion.m_info));',
     '      return portion.m_info && infoMgr.IsListItemDependentInfo(portion.m_info);',
     'computed info with a list item argument follows the selection'),
    ('not-cached', '  if (!m_listItemDependent)\n', '  if (true)\n',
     'label result is cached after the first check'),
]


def run(code, out, name):
    cpp = out / (name + '.cpp')
    cpp.write_text(code)
    exe = out / name
    build = subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                            '-fno-sanitize-recover=undefined', str(cpp), '-o', str(exe)], capture_output=True, text=True)
    if build.returncode:
        raise RuntimeError(name + ' compile failed: ' + build.stderr)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    files = {f: (args.root / f).read_text() for f in (LABEL, MANAGER, VARIABLE, LABELS)}
    code = source(files)
    with tempfile.TemporaryDirectory(prefix='gui-selection-labels-') as tmp:
        out = Path(tmp)
        good = run(code, out, 'production')
        if good.returncode or not good.stdout.startswith('PASS '):
            raise SystemExit('production failed: ' + good.stdout + good.stderr)
        print(good.stdout.strip())
        for name, old, new, witness in MUTATIONS:
            assert code.count(old) == 1, name + ' mutation anchor not unique'
            result = run(code.replace(old, new, 1), out, name)
            failed = [line for line in result.stderr.splitlines() if line.startswith('FAIL: ')]
            if result.returncode != 1 or failed != ['FAIL: ' + witness]:
                raise SystemExit(f'semantic control {name} not rejected by "{witness}": {result.returncode} {result.stderr}')
            print('PASS semantic control', name)
    print('GUI selection labels: PASS;', len(MUTATIONS), 'semantic controls rejected; host fixtures, not skin parsing or device verification')


if __name__ == '__main__':
    main()
