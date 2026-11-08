#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the production image manager and completion/cancellation methods."""
from pathlib import Path
import re
import subprocess
import tempfile

root=Path(__file__).resolve().parents[2]
def function(text, signature):
    start=text.index(signature)
    end=text.index('{',start)+1
    depth=1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}')
        end+=1
    return text[start:end]

def strip_includes(text):
    return re.sub(r'^\s*#(?:include|pragma)[^\n]*\n','',text,flags=re.M)

code=(root/'tools/async-texture-upload/ManagerFixture.h').read_text()
code+=strip_includes((root/'xbmc/GUILargeTextureManager.h').read_text()).replace('private:','public:')
code+=strip_includes((root/'xbmc/GUILargeTextureManager.cpp').read_text())
jobs=(root/'xbmc/utils/JobManager.cpp').read_text()
for signature in ['bool CJob::ShouldCancel(', 'CJob *CJobManager::PopJob()', 'unsigned int CJobManager::GetMaxWorkers(',
                  'void CJobManager::CancelJob(',
                  'bool CJobManager::OnJobProgress(', 'void CJobManager::OnJobComplete(']:
    code+=function(jobs,signature)
code+=(root/'tools/async-texture-upload/TestManager.cpp').read_text()
with tempfile.TemporaryDirectory(prefix='async-upload-manager-') as temporary:
    path=Path(temporary);(path/'test.cpp').write_text(code)
    subprocess.run(['c++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-pthread',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    str(path/'test.cpp'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True,timeout=60)
